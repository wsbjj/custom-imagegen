#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imggen.py — 用自备的 OpenAI 兼容接口生成/编辑图片（仅依赖 Python 标准库）。

子命令
  check                        校验配置、连通性，并列出疑似图片模型
  gen  "<prompt>"              文生图（默认）；带 --image 时自动转为图生图/编辑
  edit --image IMG "<prompt>"  图生图 / 图片编辑（可多张参考图，可选 --mask）
  batch --file jobs.jsonl      批量执行
  config init|show|path|set    配置文件管理

协议（--protocol）
  auto    images 优先，遇到“接口不存在”类错误时自动回退 chat（默认）
  images  POST {base}/images/generations 或 /images/edits
  chat    POST {base}/chat/completions，兼容“对话式出图”模型与中转站
  async   创建任务 + 轮询结果（创建路径、轮询路径、状态字段、结果提取均可配置）

退出码
  0 成功 | 2 配置错误 | 3 网络错误 | 4 API/HTTP 错误 | 5 任务失败 | 6 超时 | 7 写文件失败 | 8 用法错误

标准输出始终是一段 JSON（成功或失败），便于 agent 解析；人类可读的进度写在标准错误。
详细文档见 skill 目录下 references/cli.md、references/setup.md、references/protocols.md。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_NETWORK = 3
EXIT_API = 4
EXIT_TASK_FAILED = 5
EXIT_TIMEOUT = 6
EXIT_WRITE = 7
EXIT_USAGE = 8

DEFAULT_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_POLL_TIMEOUT = 900.0
DEFAULT_RETRIES = 1
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_OUT_DIR = "imggen-out"

# macOS 本机默认：用户名 -> 钥匙串 service。默认空表，真正的映射放在本地配置里
# （<配置目录>/keychain_defaults.json，例如 {"你的用户名": "ai-cmdme"}），或用
# IMGGEN_KEYCHAIN_DEFAULTS 传 JSON；这样仓库里不会出现任何个人标识。
# 只有在 profile 没有配置任何密钥来源（api_key / api_key_env / api_key_keychain / api_key_file）时才使用。
LOCAL_MACOS_KEYCHAIN_DEFAULTS = {}

DEFAULT_PATHS = {
    "models_path": "/models",
    "generation_path": "/images/generations",
    "edit_path": "/images/edits",
    "chat_path": "/chat/completions",
}

DEFAULT_ASYNC = {
    "create_path": None,
    "poll_paths": ["/images/tasks/{task_id}", "/images/generations/{task_id}", "/tasks/{task_id}"],
    "poll_method": "GET",
    "task_id_paths": ["task_id", "id", "data.task_id", "data.id", "output.task_id", "result.task_id"],
    "status_paths": [
        "status", "state", "task_status", "data.status", "data.state",
        "data.task_status", "output.task_status", "result.status",
    ],
    "progress_paths": ["progress", "data.progress", "percentage", "data.percentage"],
    "success_values": ["succeeded", "success", "completed", "complete", "done", "finished", "ok"],
    "failure_values": ["failed", "failure", "error", "canceled", "cancelled", "rejected", "expired"],
    "extra_body": {},
}

IMAGE_MODEL_HINT = re.compile(
    r"image|dall|flux|stable|sdxl|sd3|seedream|seededit|kolors|qwen-image|wanx|jimeng|"
    r"cogview|gpt-image|ideogram|recraft|midjourney|nano-banana|banana|imagen|photon|"
    r"lumina|hidream|kling|doubao-seedream|绘图|画图",
    re.I,
)
DATA_URI_RE = re.compile(r"data:image/(png|jpe?g|webp|gif|bmp|avif);base64,([A-Za-z0-9+/=\s]+)", re.I)
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)")
HTTP_URL_RE = re.compile(r"^https?://", re.I)
B64_TEXT_RE = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")
SECRET_KEY_RE = re.compile(r"api[-_]?key|authorization|token|secret|password|cookie", re.I)
ENDPOINT_MISSING_RE = re.compile(
    r"not\s*found|no\s*such|unsupported|unknown\s*(endpoint|route|path|url)|invalid\s*(endpoint|url|path)|"
    r"does\s*not\s*exist|not\s*implemented|不存在|未找到|不支持|无效.*(接口|路由|地址|endpoint)",
    re.I,
)

B64_KEYS = {
    "b64_json", "base64", "b64", "image_base64", "image_b64", "img_base64",
    "image_data", "imagebase64", "image_base64_data",
}
URL_KEYS = {"url", "image_url", "imageurl", "img_url", "image", "images", "mask_url"}

_QUIET = False
_DEBUG = False


# --------------------------------------------------------------------------- 错误与输出


class ImgGenError(Exception):
    """可预期的失败：携带 stage / 退出码 / 给用户的修复建议。"""

    def __init__(self, message, *, stage, exit_code, hint=None, http_status=None, excerpt=None, extra=None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.exit_code = exit_code
        self.hint = hint
        self.http_status = http_status
        self.excerpt = excerpt
        self.extra = extra or {}


def log(message):
    if not _QUIET:
        print(f"[imggen] {message}", file=sys.stderr, flush=True)


def warn(message):
    if not _QUIET:
        print(f"[imggen][警告] {message}", file=sys.stderr, flush=True)


def shorten(text, limit=1200):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字符）"


def mask_secret(value):
    if not value:
        return None
    value = str(value)
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}（长度 {len(value)}）"


def redact(obj, depth=0):
    """递归掩码密钥字段，保证任何日志/JSON 输出都不带出完整 key。"""
    if depth > 8:
        return "<深层结构已省略>"
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and SECRET_KEY_RE.search(key) and isinstance(value, (str, int, float)):
                out[key] = mask_secret(value)
            else:
                out[key] = redact(value, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(item, depth + 1) for item in obj[:50]]
    if isinstance(obj, str) and len(obj) > 4000:
        return obj[:4000] + f"...（已截断，共 {len(obj)} 字符）"
    return obj


def emit(obj, *, exit_code, command=None):
    """输出标准 JSON 结果并返回退出码。"""
    payload = dict(obj)
    payload.setdefault("ok", exit_code == EXIT_OK)
    if command:
        payload.setdefault("command", command)
    payload.setdefault("exit_code", exit_code)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def emit_error(command, error):
    if _DEBUG:
        import traceback

        traceback.print_exc()
    reserved = {"ok", "command", "stage", "error", "exit_code", "hint", "http_status", "response_excerpt"}
    detail = {k: redact(v) for k, v in (error.extra or {}).items() if k not in reserved}
    body = {
        "ok": False,
        "command": command,
        "stage": error.stage,
        "error": error.message,
        "exit_code": error.exit_code,
    }
    if error.http_status is not None:
        body["http_status"] = error.http_status
    if error.hint:
        body["hint"] = error.hint
    if error.excerpt:
        body["response_excerpt"] = shorten(error.excerpt)
    body.update(detail)
    print(json.dumps(body, ensure_ascii=False, indent=2))
    log(f"失败（stage={error.stage}）：{error.message}")
    if error.hint:
        log(f"建议：{error.hint}")
    return error.exit_code


# --------------------------------------------------------------------------- 通用工具


def parse_scalar(text):
    t = text.strip()
    if t == "":
        return ""
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    if t[0] in "[{":
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return t
    return t


def parse_kv_params(items, flag_name):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ImgGenError(
                f"{flag_name} 需要 key=value 形式，收到：{item}",
                stage="usage",
                exit_code=EXIT_USAGE,
                hint=f"例如：{flag_name} seed=42",
            )
        key, value = item.split("=", 1)
        out[key.strip()] = parse_scalar(value)
    return out


def parse_headers(items):
    out = {}
    for item in items or []:
        if ":" not in item:
            raise ImgGenError(
                f"--header 需要 Name:Value 形式，收到：{item}",
                stage="usage",
                exit_code=EXIT_USAGE,
                hint='例如：--header "X-Foo: bar"',
            )
        name, value = item.split(":", 1)
        out[name.strip()] = value.strip()
    return out


def get_path(obj, path):
    cur = obj
    for part in re.split(r"[.\[\]]", str(path)):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def find_key_like(obj, key_regex, depth=0):
    """深度优先查找第一个键名匹配的对象值。"""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key_regex.search(key) and isinstance(value, (str, int)):
                return value
        for value in obj.values():
            found = find_key_like(value, key_regex, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key_like(value, key_regex, depth + 1)
            if found is not None:
                return found
    return None


def sniff_image(data):
    if not data:
        return None, None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:2] == b"BM":
        return "bmp", "image/bmp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico", "image/x-icon"
    if len(data) > 16 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis", b"heic", b"mif1"):
        return "avif", "image/avif"
    return None, None


def image_size(data):
    """不依赖 Pillow 读取宽高（png/jpeg/gif/webp/bmp），失败返回 None。"""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            if chunk == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF)
            if chunk == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if data[:2] == b"BM" and len(data) >= 26:
            return int.from_bytes(data[18:22], "little"), int.from_bytes(data[22:26], "little")
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                segment_length = int.from_bytes(data[index + 2:index + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(data[index + 7:index + 9], "big"), int.from_bytes(data[index + 5:index + 7], "big")
                index += 2 + segment_length
    except Exception:
        return None
    return None


def decode_b64_loose(text):
    cleaned = re.sub(r"\s+", "", text)
    pad = len(cleaned) % 4
    if pad:
        cleaned += "=" * (4 - pad)
    try:
        return base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError):
        return None


# --------------------------------------------------------------------------- 配置


def config_dir():
    override = os.environ.get("IMGGEN_CONFIG_DIR")
    if override:
        return Path(os.path.expanduser(override))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".config"
    return base / "imggen"


def config_file():
    override = os.environ.get("IMGGEN_CONFIG")
    if override:
        return Path(os.path.expanduser(override))
    return config_dir() / "config.json"


def load_config_file():
    path = config_file()
    if not path.exists():
        return {"version": 1, "default_profile": "default", "profiles": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImgGenError(
            f"配置文件不是合法 JSON：{path}（{exc}）",
            stage="config",
            exit_code=EXIT_CONFIG,
            hint="修正该文件，或运行 imggen.py config init --force 重新生成。",
        )
    raw.setdefault("profiles", {})
    return raw


def save_config_file(cfg):
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def pick_value(cli_value, env_names, profile, profile_key, default=None, caster=None):
    value, source = None, None
    if cli_value not in (None, ""):
        value, source = cli_value, "cli"
    else:
        for name in env_names or []:
            if os.environ.get(name):
                value, source = os.environ[name], f"env:{name}"
                break
        if value is None and profile.get(profile_key) not in (None, ""):
            value, source = profile.get(profile_key), "config"
    if value in (None, ""):
        value, source = default, ("default" if default is not None else None)
    if caster is not None and value is not None:
        try:
            value = caster(value)
        except (TypeError, ValueError):
            value = default
    return value, source


def current_user():
    for name in ("USER", "LOGNAME"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def read_keychain_secret(service, account=None, *, timeout=10.0):
    """从 macOS 钥匙串读取口令（先 generic 再 internet），失败返回 None。

    可用 IMGGEN_SECURITY_BIN 覆盖 security 可执行文件（测试用）。
    """
    if sys.platform != "darwin" or not service:
        return None
    binary = os.environ.get("IMGGEN_SECURITY_BIN") or shutil.which("security") or "/usr/bin/security"
    if not os.path.exists(binary):
        return None
    commands = []
    if account:
        commands.append([binary, "find-generic-password", "-a", account, "-s", service, "-w"])
    commands.append([binary, "find-generic-password", "-s", service, "-w"])
    if account:
        commands.append([binary, "find-internet-password", "-a", account, "-s", service, "-w"])
    commands.append([binary, "find-internet-password", "-s", service, "-w"])
    for command in commands:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode == 0:
            value = (proc.stdout or "").strip()
            if value:
                return value
    return None


def local_keychain_defaults():
    """本机钥匙串默认表：内置表（空） + IMGGEN_KEYCHAIN_DEFAULTS + <配置目录>/keychain_defaults.json。"""
    defaults = {str(k): str(v) for k, v in LOCAL_MACOS_KEYCHAIN_DEFAULTS.items()}
    candidates = []
    if os.environ.get("IMGGEN_KEYCHAIN_DEFAULTS"):
        candidates.append(("IMGGEN_KEYCHAIN_DEFAULTS", os.environ["IMGGEN_KEYCHAIN_DEFAULTS"]))
    path = config_file().parent / "keychain_defaults.json"
    if path.exists():
        try:
            candidates.append((str(path), path.read_text(encoding="utf-8")))
        except OSError:
            warn(f"读取 {path} 失败，已忽略本机钥匙串默认")
    for source, text in candidates:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            warn(f"{source} 不是合法 JSON，已忽略本机钥匙串默认")
            continue
        if isinstance(parsed, dict):
            defaults.update({str(k): str(v) for k, v in parsed.items()})
    return defaults


def resolve_api_key(profile, cli_key, cli_keychain=None):
    """解析密钥来源。

    显式来源（命令行 / 环境变量 / profile 配置）按顺序尝试；只有当用户没有声明任何密钥来源时，
    才启用隐式来源：本机钥匙串默认（macOS + LOCAL_MACOS_KEYCHAIN_DEFAULTS）→ OPENAI_API_KEY。
    这样写错的钥匙串服务名或环境变量名会直接报错，而不是悄悄用了另一把密钥。
    """
    declared = bool(
        cli_key or cli_keychain or os.environ.get("IMGGEN_API_KEY")
        or profile.get("api_key") or profile.get("api_key_env")
        or profile.get("api_key_keychain") or profile.get("api_key_file")
    )
    if cli_key:
        return cli_key, "cli"
    if os.environ.get("IMGGEN_API_KEY"):
        return os.environ["IMGGEN_API_KEY"], "env:IMGGEN_API_KEY"
    if profile.get("api_key"):
        return profile["api_key"], "config"
    env_name = profile.get("api_key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name], f"env:{env_name}"

    service = cli_keychain or profile.get("api_key_keychain")
    if service:
        account = profile.get("api_key_keychain_account") or None
        source = "cli:keychain" if cli_keychain else "config:keychain"
        secret = read_keychain_secret(service, account)
        if secret:
            log(f"已从 macOS 钥匙串读取密钥（service={service}）")
            return secret, source
        warn(f"钥匙串里没读到密钥（service={service}），继续尝试其他来源")

    key_file = profile.get("api_key_file")
    if key_file:
        path = Path(os.path.expanduser(key_file))
        if path.exists():
            return path.read_text(encoding="utf-8").strip(), "config:api_key_file"

    if not declared and sys.platform == "darwin":
        default_service = local_keychain_defaults().get(current_user())
        if default_service:
            secret = read_keychain_secret(default_service, current_user())
            if secret:
                log(f"已从 macOS 钥匙串读取密钥（本机默认 service={default_service}）")
                return secret, f"keychain:default({default_service})"

    if not declared and os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"], "env:OPENAI_API_KEY"
    return None, None


def resolve_settings(args):
    cfg = load_config_file()
    profiles = cfg.get("profiles") or {}
    profile_name = (
        getattr(args, "profile", None)
        or os.environ.get("IMGGEN_PROFILE")
        or cfg.get("default_profile")
        or ("default" if "default" in profiles else (next(iter(profiles), "default")))
    )
    profile = dict(profiles.get(profile_name) or {})

    base_url, base_src = pick_value(
        getattr(args, "base_url", None), ["IMGGEN_BASE_URL", "OPENAI_BASE_URL"], profile, "base_url"
    )
    api_key, key_src = resolve_api_key(
        profile, getattr(args, "api_key", None), getattr(args, "api_key_keychain", None)
    )
    model, model_src = pick_value(getattr(args, "model", None), ["IMGGEN_MODEL"], profile, "model")
    protocol, protocol_src = pick_value(
        getattr(args, "protocol", None), ["IMGGEN_PROTOCOL"], profile, "protocol", default="auto"
    )
    timeout, _ = pick_value(
        getattr(args, "timeout", None), ["IMGGEN_TIMEOUT"], profile, "timeout",
        default=DEFAULT_TIMEOUT, caster=float,
    )
    poll_interval, _ = pick_value(
        getattr(args, "poll_interval", None), ["IMGGEN_POLL_INTERVAL"], profile, "poll_interval",
        default=DEFAULT_POLL_INTERVAL, caster=float,
    )
    poll_timeout, _ = pick_value(
        getattr(args, "poll_timeout", None), ["IMGGEN_POLL_TIMEOUT"], profile, "poll_timeout",
        default=DEFAULT_POLL_TIMEOUT, caster=float,
    )
    retries, _ = pick_value(
        getattr(args, "retries", None), ["IMGGEN_RETRIES"], profile, "retries",
        default=DEFAULT_RETRIES, caster=int,
    )
    out_dir, _ = pick_value(
        getattr(args, "out_dir", None), ["IMGGEN_OUT_DIR"], profile, "out_dir", default=DEFAULT_OUT_DIR
    )
    download_auth, _ = pick_value(
        getattr(args, "download_with_auth", None), None, profile, "download_with_auth", default="same-host"
    )

    headers = {}
    headers.update({str(k): str(v) for k, v in (profile.get("headers") or {}).items()})
    headers.update(parse_headers(getattr(args, "header", None)))
    headers.setdefault("User-Agent", f"imggen/{VERSION} (python-urllib)")

    extra_body = {}
    for source in (profile.get("extra_body") or {}, profile.get("params") or {}):
        extra_body.update(source)
    extra_body.update(parse_kv_params(getattr(args, "param", None), "--param"))
    if getattr(args, "extra_body", None):
        try:
            parsed = json.loads(args.extra_body)
        except json.JSONDecodeError as exc:
            raise ImgGenError(
                f"--extra-body 不是合法 JSON：{exc}",
                stage="usage",
                exit_code=EXIT_USAGE,
                hint="例如：--extra-body '{\"quality\": \"high\"}'",
            )
        if not isinstance(parsed, dict):
            raise ImgGenError("--extra-body 必须是 JSON 对象", stage="usage", exit_code=EXIT_USAGE)
        extra_body.update(parsed)

    async_cfg = dict(DEFAULT_ASYNC)
    async_cfg.update(profile.get("async") or {})

    settings = {
        "profile": profile_name,
        "base_url": base_url.rstrip("/") if base_url else None,
        "api_key": api_key,
        "model": model,
        "protocol": (protocol or "auto").lower(),
        "timeout": timeout,
        "poll_interval": poll_interval,
        "poll_timeout": poll_timeout,
        "retries": retries,
        "out_dir": out_dir,
        "download_with_auth": (download_auth or "same-host").lower(),
        "headers": headers,
        "extra_body": extra_body,
        "insecure": bool(getattr(args, "insecure", False)),
        "max_bytes": int(getattr(args, "max_bytes", None) or DEFAULT_MAX_BYTES),
        "response_format": (profile.get("response_format") or "auto").lower(),
        "default_size": profile.get("default_size"),
        "api_key_keychain": profile.get("api_key_keychain"),
        "api_key_keychain_account": profile.get("api_key_keychain_account"),
        "edit_transfer": (profile.get("edit_transfer") or "multipart").lower(),
        "edit_image_field": profile.get("edit_image_field"),
        "async": async_cfg,
        "sources": {"base_url": base_src, "api_key": key_src, "model": model_src, "protocol": protocol_src},
        "config_path": str(config_file()),
    }
    for key, default in DEFAULT_PATHS.items():
        settings[key] = profile.get(key) or default
    return settings


def require_config(settings, *, need_model=True):
    if not settings["base_url"]:
        raise ImgGenError(
            "缺少 base_url",
            stage="config",
            exit_code=EXIT_CONFIG,
            hint=(
                "让用户提供接口地址与密钥，然后二选一："
                "① imggen.py config init --base-url https://host/v1 --api-key sk-... --model 模型名；"
                "② 设置环境变量 IMGGEN_BASE_URL / IMGGEN_API_KEY。"
            ),
        )
    if not settings["api_key"]:
        raise ImgGenError(
            "缺少 api_key",
            stage="config",
            exit_code=EXIT_CONFIG,
            hint=(
                "让用户提供密钥，然后任选一种："
                "① imggen.py config init --base-url ... --api-key sk-...（写入 ~/.config/imggen/config.json，权限 600）；"
                "② 设置 IMGGEN_API_KEY；"
                "③ macOS 可把密钥存进钥匙串后用 api_key_keychain 指定服务名，"
                "或在 <配置目录>/keychain_defaults.json 里配置本机默认映射（形如 {\"用户名\": \"钥匙串服务名\"}）。"
                "不要把密钥写进仓库、日志或聊天记录。"
            ),
        )
    if need_model and not settings["model"]:
        raise ImgGenError(
            "缺少 model",
            stage="config",
            exit_code=EXIT_CONFIG,
            hint="先用 imggen.py check 查看可用模型列表，再用 --model 指定，或写入 profile 的 model 字段。",
        )


# --------------------------------------------------------------------------- HTTP


def join_url(base_url, path):
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base_url.rstrip("/") + "/" + path.lstrip("/")


_OPENER_CACHE = {}


def _opener(insecure):
    key = bool(insecure)
    if key not in _OPENER_CACHE:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _OPENER_CACHE[key] = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    return _OPENER_CACHE[key]


def header_value(headers, name, default=None):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return default


def http_request(method, url, *, headers=None, body=None, timeout=60.0, retries=0,
                 retry_status=(429, 502, 503, 504), insecure=False, label=None):
    """返回 (status, headers_dict, raw_bytes)。网络类错误直接抛 ImgGenError。"""
    headers = dict(headers or {})
    attempt = 0
    while True:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _opener(insecure).open(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            if exc.code in retry_status and attempt < retries:
                delay = min(8.0, 1.5 * (2 ** attempt)) + random.random() * 0.5
                log(f"HTTP {exc.code}，{delay:.1f}s 后重试（第 {attempt + 1}/{retries} 次）")
                time.sleep(delay)
                attempt += 1
                continue
            return exc.code, dict(exc.headers or {}), raw
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if attempt < retries and isinstance(reason, (TimeoutError, OSError, ssl.SSLError)):
                delay = min(8.0, 1.5 * (2 ** attempt))
                log(f"网络错误（{reason}），{delay:.1f}s 后重试（第 {attempt + 1}/{retries} 次）")
                time.sleep(delay)
                attempt += 1
                continue
            raise ImgGenError(
                f"网络请求失败：{reason}",
                stage="network",
                exit_code=EXIT_NETWORK,
                hint="检查 base_url 是否可达、DNS/代理/VPN 是否正常；必要时加 --insecure（不推荐）。",
                extra={"url": url},
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            if attempt < retries:
                delay = min(8.0, 1.5 * (2 ** attempt))
                log(f"连接异常（{exc}），{delay:.1f}s 后重试（第 {attempt + 1}/{retries} 次）")
                time.sleep(delay)
                attempt += 1
                continue
            raise ImgGenError(
                f"连接失败：{exc}",
                stage="network",
                exit_code=EXIT_NETWORK,
                hint="确认 base_url、端口、网络与代理设置。",
                extra={"url": url},
            )


def parse_json_body(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def api_error_from(status, raw, *, url, label, retry_after=None):
    text = raw.decode("utf-8", errors="replace") if raw else ""
    parsed = parse_json_body(raw)
    message = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("code") or err.get("type")
        elif isinstance(err, str):
            message = err
        message = message or parsed.get("message") or parsed.get("msg") or parsed.get("detail")
    if not message:
        message = shorten(text.strip(), 400) or f"HTTP {status}"
    hints = {
        400: "请求参数被拒绝：换一个 size/模型，或把可疑的 --param 去掉后重试。",
        401: "鉴权失败：确认 api_key 是否正确、是否需要 Bearer 前缀、是否与 base_url 属于同一服务。",
        403: "无权限：确认密钥是否已开通该模型，或是否被地区/白名单限制。",
        404: "路径或模型不存在：base_url 通常要包含 /v1；也可能该服务不提供此端点，可试 --protocol chat。",
        413: "请求体或图片过大：压缩参考图后重试。",
        422: "参数校验失败：核对模型名与参数名（部分服务用 image_size 而不是 size）。",
        429: "触发限流或余额不足：稍后重试，或检查账户额度。",
        500: "服务端错误：稍后重试。",
        502: "网关错误：可能是上游超时，稍后重试。",
        503: "服务不可用：稍后重试。",
        504: "网关超时：稍后重试或改用异步协议（--protocol async）。",
    }
    hint = hints.get(status, "查看 response_excerpt 的原始返回，必要时用 --dry-run 检查请求体。")
    if retry_after:
        hint = f"{hint}（服务端建议 {retry_after}s 后重试）"
    return ImgGenError(
        f"HTTP {status}：{message}",
        stage="api",
        exit_code=EXIT_API,
        http_status=status,
        excerpt=text,
        hint=hint,
        extra={"url": url, "label": label},
    )


# --------------------------------------------------------------------------- 请求构造


def build_common_headers(settings, *, json_body=True, accept="application/json"):
    headers = dict(settings["headers"])
    headers["Authorization"] = f"Bearer {settings['api_key']}"
    headers["Accept"] = accept
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def encode_multipart(fields, files):
    boundary = "----imggen" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        text = str(value) if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False)
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        body += text.encode("utf-8") + b"\r\n"
    for field_name, filename, content, content_type in files:
        safe_name = str(filename).replace('"', "_").replace("\\", "_")
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def as_data_uri(path_or_url):
    if HTTP_URL_RE.match(str(path_or_url)):
        return path_or_url
    path = Path(os.path.expanduser(str(path_or_url)))
    if not path.exists():
        raise ImgGenError(
            f"参考图不存在：{path}",
            stage="input",
            exit_code=EXIT_USAGE,
            hint="确认 --image 路径是否正确（支持本地路径或 http(s) URL）。",
        )
    data = path.read_bytes()
    _, mime = sniff_image(data)
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(data).decode('ascii')}"


def merge_payload(base, settings, cli_params=None):
    merged = dict(base)
    merged.update(settings.get("extra_body") or {})
    merged.update(cli_params or {})
    return {key: value for key, value in merged.items() if value is not None}


def build_images_payload(settings, request):
    payload = {"model": request["model"], "prompt": request["prompt"]}
    if request.get("count") and int(request["count"]) > 1:
        payload["n"] = int(request["count"])
    if request.get("size"):
        payload["size"] = request["size"]
    if settings["response_format"] in ("b64_json", "url"):
        payload["response_format"] = settings["response_format"]
    return payload


def build_chat_messages(request):
    parts = [{"type": "text", "text": request["prompt"]}]
    for image in request.get("images") or []:
        parts.append({"type": "image_url", "image_url": {"url": as_data_uri(image)}})
    return [{"role": "user", "content": parts if len(parts) > 1 else request["prompt"]}]


# --------------------------------------------------------------------------- 结果提取


def scan_string(value, path, out):
    match = DATA_URI_RE.search(value)
    if match:
        out.append({
            "kind": "b64", "value": re.sub(r"\s+", "", match.group(2)),
            "source": path, "mime": f"image/{match.group(1).lower()}",
        })
    for url in MD_IMAGE_RE.findall(value):
        out.append({"kind": "url", "value": url, "source": path})
    if not match and len(value) >= 100 and B64_TEXT_RE.fullmatch(value):
        # 没有明确字段名时，只有真正能解出图片才当作候选，避免把长文本误判成 base64。
        decoded = decode_b64_loose(value)
        if decoded and sniff_image(decoded)[0]:
            out.append({"kind": "b64", "value": value, "source": path, "maybe": True})


def extract_candidates(obj, path="", out=None, depth=0):
    out = [] if out is None else out
    if depth > 30:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            key_lower = str(key).lower()
            if isinstance(value, str):
                if key_lower in B64_KEYS and len(value) >= 16:
                    out.append({"kind": "b64", "value": value, "source": child})
                elif key_lower in URL_KEYS and HTTP_URL_RE.match(value.strip()):
                    out.append({"kind": "url", "value": value.strip(), "source": child})
                elif key_lower in URL_KEYS and len(value) >= 100 and B64_TEXT_RE.fullmatch(value.strip()):
                    decoded = decode_b64_loose(value)
                    if decoded and sniff_image(decoded)[0]:
                        out.append({"kind": "b64", "value": value, "source": child, "maybe": True})
                else:
                    scan_string(value, child, out)
            else:
                extract_candidates(value, child, out, depth + 1)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child = f"{path}.{index}"
            if isinstance(value, str):
                scan_string(value, child, out)
            else:
                extract_candidates(value, child, out, depth + 1)
    return out


def dedupe_candidates(candidates):
    seen, result = set(), []
    for item in candidates:
        value = re.sub(r"\s+", "", item["value"]) if item["kind"] == "b64" else item["value"]
        fingerprint = (item["kind"], value[:96], len(value))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        item = dict(item)
        item["value"] = value
        result.append(item)
    return result


def unique_path(path):
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}")


def slugify(text, limit=32):
    text = unicodedata.normalize("NFKD", str(text))
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", text).strip("-")
    return (text[:limit] or "img").lower()


def download_url(settings, url):
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlparse(settings["base_url"] or "")
    mode = settings["download_with_auth"]
    headers = {"User-Agent": settings["headers"].get("User-Agent", f"imggen/{VERSION}")}
    if mode == "always" or (mode == "same-host" and parsed.netloc == base.netloc):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    status, _, raw = http_request(
        "GET", url, headers=headers, timeout=settings["timeout"], retries=settings["retries"],
        insecure=settings["insecure"], label="download",
    )
    if status >= 400:
        raise ImgGenError(
            f"下载图片失败：HTTP {status}",
            stage="download",
            exit_code=EXIT_NETWORK,
            http_status=status,
            excerpt=raw.decode("utf-8", errors="replace") if raw else None,
            hint="图片链接可能已过期或需要鉴权；可加 --download-with-auth always 重试。",
            extra={"url": url},
        )
    return raw


def materialize_images(candidates, settings, *, out_dir, prefix, want_format, download=True):
    """把候选（url / b64）落盘，返回 (images, warnings)。"""
    images, warnings = [], []
    out_path = Path(os.path.expanduser(out_dir)) if out_dir else Path(settings["out_dir"])
    if download:
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ImgGenError(
                f"无法创建输出目录：{out_path}（{exc}）",
                stage="output",
                exit_code=EXIT_WRITE,
                hint="换一个可写目录，例如 --out-dir /tmp/imggen，或检查目录权限。",
            )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base_prefix = prefix or "img"

    for index, candidate in enumerate(candidates, start=1):
        data = None
        source_kind = candidate["kind"]
        if source_kind == "b64":
            data = decode_b64_loose(candidate["value"])
            if data is None:
                warnings.append(f"无法解码 base64 数据（来源 {candidate.get('source')}），已跳过。")
                continue
        elif not download:
            images.append({
                "path": None, "url": candidate["value"], "source": candidate.get("source"), "index": index,
                "width": None, "height": None, "bytes": None, "format": None,
            })
            continue
        else:
            try:
                data = download_url(settings, candidate["value"])
            except ImgGenError as exc:
                if len(candidates) == 1:
                    raise
                warnings.append(f"下载失败（{candidate['value']}）：{exc.message}")
                continue

        fmt, _ = sniff_image(data)
        if fmt is None:
            if len(candidates) == 1 and candidate["kind"] == "b64":
                raise ImgGenError(
                    "服务端返回的数据不是可识别的图片格式",
                    stage="output",
                    exit_code=EXIT_WRITE,
                    hint="确认模型名是否正确（可能返回了文本而不是图片）；数据开头：" + shorten(data[:80].hex(), 200),
                )
            warnings.append(f"返回内容不是已知图片格式，已跳过（来源 {candidate.get('source')}）。")
            continue

        if len(data) > settings["max_bytes"]:
            raise ImgGenError(
                f"图片体积 {len(data)} 字节超过上限 {settings['max_bytes']}",
                stage="output",
                exit_code=EXIT_WRITE,
                hint="用 --max-bytes 提高上限，或让服务端返回更小的尺寸/质量。",
            )

        ext = fmt
        if want_format and want_format != "auto" and want_format != fmt:
            converted = try_convert_with_pil(data, want_format)
            if converted is not None:
                data, fmt, ext = converted, want_format, want_format
            else:
                warnings.append(f"服务端返回 {fmt}，转成 {want_format} 需要 Pillow；已按 {fmt} 保存。")

        size = image_size(data) or (None, None)
        if not download:
            images.append({
                "path": None, "url": candidate.get("value"), "source": candidate.get("source"),
                "index": index, "bytes": len(data), "format": fmt, "width": size[0], "height": size[1],
            })
            continue

        suffix = ".jpg" if ext == "jpeg" else f".{ext}"
        target = unique_path(out_path / f"{slugify(base_prefix)}-{stamp}-{index}{suffix}")
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise ImgGenError(
                f"写入图片失败：{target}（{exc}）",
                stage="output",
                exit_code=EXIT_WRITE,
                hint="检查目录权限与磁盘空间，或用 --out-dir 指定其他目录。",
            )
        images.append({
            "path": str(target.resolve()),
            "url": candidate.get("value") if source_kind == "url" else None,
            "source": candidate.get("source"),
            "index": index,
            "bytes": len(data),
            "width": size[0],
            "height": size[1],
            "format": fmt,
        })
        log(f"已保存：{target.resolve()}（{fmt}, {size[0] or '?'}x{size[1] or '?'}, {len(data) / 1024:.0f} KB）")

    if not images and not warnings:
        warnings.append("没有可落盘的图片数据。")
    return images, warnings


def try_convert_with_pil(data, target_format):
    try:
        from io import BytesIO

        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        image = Image.open(BytesIO(data))
        if target_format == "jpeg" and image.mode in ("RGBA", "P", "LA"):
            image = image.convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="JPEG" if target_format == "jpeg" else target_format.upper())
        return buffer.getvalue()
    except Exception:
        return None


# --------------------------------------------------------------------------- 协议实现


class EndpointUnavailable(Exception):
    """当前协议对应的端点不存在，可回退其他协议。"""


def looks_like_endpoint_missing(status, raw):
    if status in (404, 405):
        return True
    if status == 400:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return bool(ENDPOINT_MISSING_RE.search(text))
    return False


def load_local_image(path_or_url, fallback_name=None):
    if HTTP_URL_RE.match(str(path_or_url)):
        parsed = urllib.parse.urlparse(str(path_or_url))
        name = Path(parsed.path).name or (fallback_name or "image.png")
        status, _, raw = http_request("GET", str(path_or_url), retries=1, timeout=120, label="fetch-input")
        if status >= 400:
            raise ImgGenError(
                f"下载参考图失败：HTTP {status}",
                stage="input",
                exit_code=EXIT_NETWORK,
                hint="参考图 URL 需要可公开访问，或改为传本地文件路径。",
            )
        return raw, name, sniff_image(raw)[1] or "image/png"
    path = Path(os.path.expanduser(str(path_or_url)))
    if not path.exists():
        raise ImgGenError(
            f"参考图不存在：{path}",
            stage="input",
            exit_code=EXIT_USAGE,
            hint="确认 --image 路径是否正确（支持本地路径或 http(s) URL）。",
        )
    data = path.read_bytes()
    return data, path.name or (fallback_name or "image.png"), sniff_image(data)[1] or "image/png"


def request_images_protocol(settings, request, *, dry_run):
    """images 协议：无参考图走 /images/generations；有参考图走 /images/edits。"""
    has_images = bool(request.get("images") or request.get("mask"))
    path = settings["edit_path"] if has_images else settings["generation_path"]
    url = join_url(settings["base_url"], path)
    payload = build_images_payload(settings, request)
    payload = merge_payload(payload, settings, request.get("params"))

    if has_images and settings["edit_transfer"] == "base64":
        body_obj = dict(payload)
        image_values = [as_data_uri(item) for item in request.get("images") or []]
        if image_values:
            body_obj["image"] = image_values if len(image_values) > 1 else image_values[0]
        if request.get("mask"):
            body_obj["mask"] = as_data_uri(request["mask"])
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        headers = build_common_headers(settings)
    elif has_images:
        files = []
        field = settings["edit_image_field"] or ("image" if len(request.get("images") or []) == 1 else "image[]")
        for item in request.get("images") or []:
            data, name, mime = load_local_image(item)
            files.append((field, name, data, mime))
        if request.get("mask"):
            data, name, mime = load_local_image(request["mask"], fallback_name="mask.png")
            files.append(("mask", name, data, mime))
        body, content_type = encode_multipart(payload, files)
        headers = build_common_headers(settings, json_body=False)
        headers["Content-Type"] = content_type
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = build_common_headers(settings)

    meta = {"url": url, "method": "POST", "payload": redact(payload), "has_images": has_images,
            "request_bytes": len(body)}
    if dry_run:
        return {"images": [], "candidates": [], "meta": meta, "dry_run": True}

    log(f"POST {url}（protocol=images, timeout={settings['timeout']:.0f}s）")
    status, resp_headers, raw = http_request(
        "POST", url, headers=headers, body=body, timeout=settings["timeout"],
        retries=settings["retries"], insecure=settings["insecure"], label="images",
    )
    parsed = parse_json_body(raw)
    if status >= 400:
        if looks_like_endpoint_missing(status, raw):
            raise EndpointUnavailable(f"HTTP {status}：{shorten(raw.decode('utf-8', errors='replace'), 300)}")
        raise api_error_from(status, raw, url=url, label="images",
                             retry_after=header_value(resp_headers, "Retry-After"))
    if parsed is None:
        raise ImgGenError(
            f"服务端返回非 JSON（HTTP {status}）",
            stage="api",
            exit_code=EXIT_API,
            excerpt=raw.decode("utf-8", errors="replace"),
            hint="确认该地址是 API 端点而不是网页；base_url 通常以 /v1 结尾。",
            extra={"url": url},
        )
    candidates = dedupe_candidates(extract_candidates(parsed))
    if isinstance(parsed, dict) and parsed.get("usage"):
        meta["usage"] = redact(parsed["usage"])
    if not candidates:
        raise ImgGenError(
            "接口返回成功，但没有找到图片数据",
            stage="api",
            exit_code=EXIT_API,
            excerpt=json.dumps(redact(parsed), ensure_ascii=False)[:1500],
            hint="可能是模型名不支持出图、需要额外参数（--param），或返回结构特殊；可试 --protocol chat。",
            extra={"url": url},
        )
    return {"images": [], "candidates": candidates, "meta": meta, "raw_response": parsed}


def request_chat_protocol(settings, request, *, dry_run):
    url = join_url(settings["base_url"], settings["chat_path"])
    payload = {"model": request["model"], "messages": build_chat_messages(request)}
    if request.get("count") and int(request["count"]) > 1:
        payload["n"] = int(request["count"])
    payload = merge_payload(payload, settings, request.get("params"))

    warnings = []
    if request.get("mask"):
        warnings.append("chat 协议不支持 mask 参数；蒙版未上传，仅按提示词处理。")

    meta = {"url": url, "method": "POST", "payload": redact(payload), "warnings": warnings}
    if dry_run:
        return {"images": [], "candidates": [], "meta": meta, "dry_run": True, "warnings": warnings}

    log(f"POST {url}（protocol=chat, timeout={settings['timeout']:.0f}s）")
    status, resp_headers, raw = http_request(
        "POST", url, headers=build_common_headers(settings),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=settings["timeout"], retries=settings["retries"],
        insecure=settings["insecure"], label="chat",
    )
    parsed = parse_json_body(raw)
    if status >= 400:
        if looks_like_endpoint_missing(status, raw):
            raise EndpointUnavailable(f"HTTP {status}：{shorten(raw.decode('utf-8', errors='replace'), 300)}")
        raise api_error_from(status, raw, url=url, label="chat",
                             retry_after=header_value(resp_headers, "Retry-After"))
    if parsed is None:
        raise ImgGenError(
            f"服务端返回非 JSON（HTTP {status}）",
            stage="api",
            exit_code=EXIT_API,
            excerpt=raw.decode("utf-8", errors="replace"),
            hint="确认 base_url 与模型是否支持 /chat/completions。",
            extra={"url": url},
        )

    candidates = extract_candidates(parsed)
    if isinstance(parsed, dict):
        for choice in parsed.get("choices") or []:
            message = (choice or {}).get("message") or {}
            text = message.get("content")
            if isinstance(text, str):
                scan_string(text, "choices.message.content", candidates)
                for url_match in re.findall(r"https?://[^\s)\]}]+", text):
                    if re.search(r"\.(png|jpe?g|webp|gif)(\?|#|$)", url_match, re.I):
                        candidates.append({"kind": "url", "value": url_match, "source": "choices.message.content"})
            for key in ("images", "image_urls", "image"):
                if isinstance(message.get(key), (list, dict)):
                    extract_candidates(message[key], f"choices.message.{key}", candidates)
    candidates = dedupe_candidates(candidates)

    if not candidates:
        preview = None
        if isinstance(parsed, dict) and parsed.get("choices"):
            preview = shorten(str(parsed["choices"][0].get("message", {}).get("content")), 600)
        raise ImgGenError(
            "对话接口返回成功，但没有解析到图片",
            stage="api",
            exit_code=EXIT_API,
            excerpt=preview or json.dumps(redact(parsed), ensure_ascii=False)[:1200],
            hint="确认模型名支持出图（如 nano-banana / gpt-image / qwen-image），或加：--param 'modalities=[\"image\",\"text\"]'",
            extra={"url": url},
        )
    return {"images": [], "candidates": candidates, "meta": meta, "raw_response": parsed, "warnings": warnings}


def request_async_protocol(settings, request, *, dry_run):
    async_cfg = settings["async"]
    create_path = async_cfg.get("create_path") or settings["generation_path"]
    create_url = join_url(settings["base_url"], create_path)
    payload = build_images_payload(settings, request)
    if request.get("images") or request.get("mask"):
        image_values = [as_data_uri(item) for item in request.get("images") or []]
        if image_values:
            payload["image"] = image_values if len(image_values) > 1 else image_values[0]
        if request.get("mask"):
            payload["mask"] = as_data_uri(request["mask"])
    payload = merge_payload(payload, settings, request.get("params"))
    payload.update(async_cfg.get("extra_body") or {})

    meta = {"url": create_url, "method": "POST", "payload": redact(payload)}
    if dry_run:
        return {"images": [], "candidates": [], "meta": meta, "dry_run": True}

    log(f"POST {create_url}（protocol=async，创建任务）")
    status, resp_headers, raw = http_request(
        "POST", create_url, headers=build_common_headers(settings),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=settings["timeout"], retries=settings["retries"],
        insecure=settings["insecure"], label="async-create",
    )
    parsed = parse_json_body(raw)
    if status >= 400:
        raise api_error_from(status, raw, url=create_url, label="async-create",
                             retry_after=header_value(resp_headers, "Retry-After"))
    if parsed is None:
        raise ImgGenError(
            "创建任务返回非 JSON",
            stage="api",
            exit_code=EXIT_API,
            excerpt=raw.decode("utf-8", errors="replace"),
            hint="确认 profile 的 async.create_path 是否正确。",
            extra={"url": create_url},
        )

    immediate = dedupe_candidates(extract_candidates(parsed))
    task_id = None
    for candidate_path in async_cfg["task_id_paths"]:
        value = get_path(parsed, candidate_path)
        if isinstance(value, (str, int)) and str(value).strip():
            task_id = str(value).strip()
            break
    if task_id is None:
        found = find_key_like(parsed, re.compile(r"(task|job|creation|request|generation)[-_]?id$", re.I))
        if found is not None:
            task_id = str(found).strip()
    if task_id is None:
        if immediate:
            meta["note"] = "创建接口直接返回了结果，无需轮询"
            return {"images": [], "candidates": immediate, "meta": meta, "raw_response": parsed}
        raise ImgGenError(
            "创建任务成功，但没有找到任务 ID",
            stage="api",
            exit_code=EXIT_API,
            excerpt=json.dumps(redact(parsed), ensure_ascii=False)[:1200],
            hint="把返回结构里的任务 ID 字段路径写进 profile 的 async.task_id_paths。",
            extra={"url": create_url},
        )

    meta["task_id"] = task_id
    log(f"任务已创建：task_id={task_id}，开始轮询（间隔 {settings['poll_interval']:.0f}s，上限 {settings['poll_timeout']:.0f}s）")

    poll_paths = list(async_cfg.get("poll_paths") or [f"{settings['generation_path']}/{{task_id}}"])
    poll_method = str(async_cfg.get("poll_method") or "GET").upper()
    poll_headers = build_common_headers(settings, json_body=(poll_method == "POST"))

    started, polls, last_status, path_index = time.time(), 0, None, 0
    while True:
        elapsed = time.time() - started
        if elapsed > settings["poll_timeout"]:
            raise ImgGenError(
                f"任务 {task_id} 超时（等待 {elapsed:.0f}s，最后状态：{last_status or '未知'}）",
                stage="poll",
                exit_code=EXIT_TIMEOUT,
                hint="任务可能仍在服务端继续；可用更大的 --poll-timeout 重试，或换更快的模型/更小尺寸。",
                extra={"task_id": task_id, "last_status": last_status},
            )
        if path_index >= len(poll_paths):
            raise ImgGenError(
                "轮询接口全部返回 404",
                stage="poll",
                exit_code=EXIT_API,
                hint="在 profile 的 async.poll_paths 里填入正确查询路径（用 {task_id} 占位）。",
                extra={"task_id": task_id, "tried": poll_paths},
            )
        poll_url = join_url(settings["base_url"], poll_paths[path_index].replace("{task_id}", urllib.parse.quote(task_id)))
        status, _, raw = http_request(
            poll_method, poll_url, headers=poll_headers, body=None,
            timeout=min(60.0, settings["timeout"]), retries=settings["retries"],
            insecure=settings["insecure"], label="async-poll",
        )
        if status == 404:
            path_index += 1
            continue
        if status >= 400:
            raise api_error_from(status, raw, url=poll_url, label="async-poll")
        body = parse_json_body(raw)
        if body is None:
            raise ImgGenError(
                "轮询返回非 JSON",
                stage="poll",
                exit_code=EXIT_API,
                excerpt=raw.decode("utf-8", errors="replace"),
                extra={"task_id": task_id, "url": poll_url},
            )
        polls += 1

        status_value = None
        for candidate_path in async_cfg["status_paths"]:
            value = get_path(body, candidate_path)
            if isinstance(value, (str, int)) and str(value).strip() != "":
                status_value = str(value).strip()
                break
        if status_value:
            last_status = status_value
        lowered = (status_value or "").lower()
        candidates = dedupe_candidates(extract_candidates(body))

        if lowered and any(lowered == item.lower() for item in async_cfg["failure_values"]):
            raise ImgGenError(
                f"任务失败：{status_value}",
                stage="task",
                exit_code=EXIT_TASK_FAILED,
                excerpt=json.dumps(redact(body), ensure_ascii=False)[:1500],
                hint="常见原因：提示词被安全策略拦截、参考图不合规、参数不被模型支持。调整后重试。",
                extra={"task_id": task_id, "polls": polls},
            )
        succeeded = bool(lowered) and any(lowered == item.lower() for item in async_cfg["success_values"])
        if (succeeded and candidates) or (candidates and not lowered):
            meta.update({"polls": polls, "elapsed_seconds": round(elapsed, 1), "status": status_value})
            return {"images": [], "candidates": candidates, "meta": meta, "raw_response": body}
        if succeeded and not candidates:
            raise ImgGenError(
                "任务显示成功，但没有解析到图片",
                stage="task",
                exit_code=EXIT_API,
                excerpt=json.dumps(redact(body), ensure_ascii=False)[:1500],
                hint="检查 async.poll_paths 是否指向结果接口；必要时把返回结构反馈给维护者。",
                extra={"task_id": task_id},
            )

        progress = None
        for candidate_path in async_cfg["progress_paths"]:
            value = get_path(body, candidate_path)
            if isinstance(value, (int, float, str)):
                progress = value
                break
        progress_text = f" progress={progress}" if progress is not None else ""
        log(f"轮询 #{polls}：status={status_value or '未知'}{progress_text} 已等待 {elapsed:.0f}s")
        time.sleep(settings["poll_interval"])


def execute_request(settings, request, *, dry_run=False):
    protocol = settings["protocol"]
    order = {"images": ["images"], "chat": ["chat"], "async": ["async"]}.get(protocol) or ["images", "chat"]
    handlers = {
        "images": request_images_protocol,
        "chat": request_chat_protocol,
        "async": request_async_protocol,
    }
    last_error = None
    for index, name in enumerate(order):
        try:
            result = handlers[name](settings, request, dry_run=dry_run)
            result["protocol_used"] = name
            if last_error is not None:
                result.setdefault("warnings", []).append(
                    f"images 协议不可用（{last_error}），已自动回退到 {name} 协议。"
                )
            return result
        except EndpointUnavailable as exc:
            last_error = shorten(str(exc), 200)
            if index + 1 >= len(order):
                raise ImgGenError(
                    f"{name} 协议端点不可用：{last_error}",
                    stage="api",
                    exit_code=EXIT_API,
                    hint="确认 base_url 是否包含 /v1 等前缀，并检查 profile 里的 generation_path / edit_path / chat_path。",
                )
            log(f"{name} 协议端点不可用，回退尝试其他协议…")
    raise ImgGenError("没有可用协议", stage="config", exit_code=EXIT_CONFIG, hint="用 --protocol images|chat|async 指定。")


# --------------------------------------------------------------------------- 命令


def build_request(args, settings, *, command):
    prompt = getattr(args, "prompt", None) or getattr(args, "prompt_option", None)
    if not prompt and getattr(args, "prompt_file", None):
        path = Path(os.path.expanduser(args.prompt_file))
        if not path.exists():
            raise ImgGenError(f"提示词文件不存在：{path}", stage="usage", exit_code=EXIT_USAGE)
        prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ImgGenError(
            "缺少提示词",
            stage="usage",
            exit_code=EXIT_USAGE,
            hint='把描述作为位置参数传入，例如：imggen.py gen "一只在雪地里的柴犬，写实摄影"。',
        )
    images = list(getattr(args, "image", None) or [])
    if getattr(args, "image_url", None):
        images.extend(args.image_url)
    missing = [
        item for item in images
        if not HTTP_URL_RE.match(str(item)) and not Path(os.path.expanduser(str(item))).exists()
    ]
    if missing:
        raise ImgGenError(
            f"参考图不存在：{', '.join(missing)}",
            stage="input",
            exit_code=EXIT_USAGE,
            hint="支持本地路径或可公开访问的 http(s) 链接。",
        )
    if command == "edit" and not images:
        raise ImgGenError(
            "edit 至少需要一张参考图（--image）",
            stage="usage",
            exit_code=EXIT_USAGE,
            hint="只做文生图时请用 gen；若要在聊天协议里用 URL 参考图，可加 --image-url。",
        )
    return {
        "prompt": prompt,
        "images": images,
        "mask": getattr(args, "mask", None),
        "count": getattr(args, "count", None) or 1,
        "size": getattr(args, "size", None) or settings.get("default_size"),
        "model": getattr(args, "model", None) or settings["model"],
        "params": parse_kv_params(getattr(args, "param", None), "--param"),
        "prefix": getattr(args, "prefix", None),
        "out_dir": getattr(args, "out_dir", None),
        "format": getattr(args, "format", None),
        "download": not getattr(args, "no_download", False),
        "command": command,
    }


def run_single(settings, request, *, dry_run):
    started = time.time()
    result = execute_request(settings, request, dry_run=dry_run)
    warnings = list(result.get("warnings") or [])
    images = []
    if not dry_run:
        images, extra_warnings = materialize_images(
            result.get("candidates") or [],
            settings,
            out_dir=request.get("out_dir") or settings["out_dir"],
            prefix=request.get("prefix") or slugify(request["prompt"], 24),
            want_format=request.get("format") or "auto",
            download=request.get("download", True),
        )
        warnings.extend(extra_warnings)
    payload = {
        "ok": True,
        "command": request.get("command", "gen"),
        "profile": settings["profile"],
        "protocol": result.get("protocol_used"),
        "model": request["model"],
        "prompt": request["prompt"],
        "elapsed_seconds": round(time.time() - started, 1),
        "request": result.get("meta"),
        "images": images,
        "warnings": warnings,
    }
    if result.get("dry_run"):
        payload["dry_run"] = True
        payload["note"] = "仅构造请求并输出，未实际发送；去掉 --dry-run 才会真正调用。"
    return payload


def cmd_generate(args, *, command):
    settings = resolve_settings(args)
    require_config(settings)
    request = build_request(args, settings, command=command)
    payload = run_single(settings, request, dry_run=bool(getattr(args, "dry_run", False)))
    return emit(payload, exit_code=EXIT_OK, command=command)


def cmd_check(args):
    settings = resolve_settings(args)
    started = time.time()
    info = {
        "ok": True,
        "command": "check",
        "profile": settings["profile"],
        "config_path": settings["config_path"],
        "base_url": settings["base_url"],
        "api_key": mask_secret(settings["api_key"]),
        "sources": settings["sources"],
        "model": settings["model"],
        "protocol": settings["protocol"],
        "default_size": settings["default_size"],
        "timeout": settings["timeout"],
        "poll_timeout": settings["poll_timeout"],
        "warnings": [],
    }
    problems = []
    if not settings["base_url"]:
        problems.append("缺少 base_url")
    if not settings["api_key"]:
        problems.append("缺少 api_key")
    if not settings["model"]:
        info["warnings"].append("尚未设置默认模型；调用时需要 --model 或在 profile 中设置 model。")
    if problems:
        raise ImgGenError(
            "；".join(problems),
            stage="config",
            exit_code=EXIT_CONFIG,
            hint=(
                "让用户提供接口地址与密钥，然后："
                "imggen.py config init --base-url https://host/v1 --api-key sk-... --model 模型名，"
                "或设置 IMGGEN_BASE_URL / IMGGEN_API_KEY 环境变量。"
            ),
            extra=info,
        )

    url = join_url(settings["base_url"], settings["models_path"])
    log(f"GET {url}（校验连通性）")
    status, _, raw = http_request(
        "GET", url, headers=build_common_headers(settings), timeout=min(60.0, settings["timeout"]),
        retries=settings["retries"], insecure=settings["insecure"], label="models",
    )
    info["models_endpoint"] = {"url": url, "status": status}
    if status < 400:
        parsed = parse_json_body(raw)
        ids = [
            str(item.get("id")) for item in (parsed or {}).get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
        image_like = [item for item in ids if IMAGE_MODEL_HINT.search(item)]
        info["models"] = {
            "total": len(ids),
            "image_like": image_like[:60],
            "hint": "列表里的图片类模型建议优先尝试；未列出不代表不可用。",
        }
        if not image_like:
            info["warnings"].append("模型列表里没有明显的图片模型，但仍可用 --model 指定任意模型名。")
    else:
        parsed = parse_json_body(raw)
        info["models_endpoint"]["error"] = shorten(
            json.dumps(redact(parsed), ensure_ascii=False) if parsed else raw.decode("utf-8", errors="replace"), 400
        )
        if status in (401, 403):
            raise api_error_from(status, raw, url=url, label="models")
        info["warnings"].append(
            f"/models 返回 HTTP {status}：部分服务不提供模型列表，这不代表不能出图；可直接用 gen 试跑一张。"
        )
    info["elapsed_seconds"] = round(time.time() - started, 1)
    return emit(info, exit_code=EXIT_OK, command="check")


def cmd_batch(args):
    settings = resolve_settings(args)
    require_config(settings)
    source = args.file
    if source == "-":
        lines = sys.stdin.read().splitlines()
    else:
        path = Path(os.path.expanduser(source))
        if not path.exists():
            raise ImgGenError(f"任务文件不存在：{path}", stage="usage", exit_code=EXIT_USAGE)
        lines = path.read_text(encoding="utf-8").splitlines()

    jobs = []
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImgGenError(
                f"第 {lineno} 行不是合法 JSON：{exc}",
                stage="usage",
                exit_code=EXIT_USAGE,
                hint='每行一个 JSON 对象，例如 {"prompt": "一只猫", "size": "1024x1024"}',
            )
        if not isinstance(job, dict):
            raise ImgGenError(f"第 {lineno} 行必须是 JSON 对象", stage="usage", exit_code=EXIT_USAGE)
        jobs.append((lineno, job))
    if not jobs:
        raise ImgGenError("任务文件里没有有效任务", stage="usage", exit_code=EXIT_USAGE)

    def run_job(index, job):
        request = {
            "prompt": job.get("prompt") or job.get("text") or "",
            "images": job.get("images") or ([job["image"]] if job.get("image") else []),
            "mask": job.get("mask"),
            "count": job.get("count") or job.get("n") or 1,
            "size": job.get("size"),
            "model": job.get("model") or settings["model"],
            "params": job.get("params") or {},
            "prefix": job.get("prefix") or f"job{index}",
            "out_dir": job.get("out_dir"),
            "format": job.get("format"),
            "download": job.get("download", True),
            "command": "batch",
        }
        if not request["prompt"]:
            raise ImgGenError(f"第 {index} 个任务缺少 prompt", stage="usage", exit_code=EXIT_USAGE)
        if not request["model"]:
            raise ImgGenError(f"第 {index} 个任务缺少 model（profile 里也没设置）", stage="config", exit_code=EXIT_CONFIG)
        return run_single(settings, request, dry_run=bool(getattr(args, "dry_run", False)))

    results, failures = [], 0
    concurrency = max(1, int(args.concurrency or 1))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run_job, lineno, job): lineno for lineno, job in jobs}
        for future in as_completed(futures):
            lineno = futures[future]
            try:
                payload = future.result()
                payload["line"] = lineno
                results.append(payload)
            except ImgGenError as exc:
                failures += 1
                results.append({
                    "ok": False, "line": lineno, "stage": exc.stage, "error": exc.message,
                    "exit_code": exc.exit_code, "hint": exc.hint,
                })
                if args.fail_fast:
                    break
            except Exception as exc:  # 兜底：批量任务不因单个任务崩溃
                failures += 1
                results.append({"ok": False, "line": lineno, "stage": "internal",
                                "error": f"{type(exc).__name__}: {exc}"})

    results.sort(key=lambda item: item.get("line") or 0)
    succeeded = sum(1 for item in results if item.get("ok"))
    payload = {
        "ok": failures == 0,
        "command": "batch",
        "profile": settings["profile"],
        "total": len(jobs),
        "succeeded": succeeded,
        "failed": failures,
        "results": results,
    }
    return emit(payload, exit_code=EXIT_OK if failures == 0 else EXIT_API, command="batch")


def cmd_config(args):
    action = args.config_action
    path = config_file()
    if action == "path":
        return emit({"ok": True, "command": "config", "action": "path", "config_path": str(path)},
                    exit_code=EXIT_OK, command="config")

    if action == "init":
        if path.exists() and not args.force:
            return emit(
                {
                    "ok": False, "command": "config", "action": "init", "config_path": str(path),
                    "error": "配置文件已存在（未覆盖）", "exit_code": EXIT_CONFIG,
                    "hint": "确认要覆盖时加 --force；只想改单项可用 config set key=value。",
                },
                exit_code=EXIT_CONFIG, command="config",
            )
        cfg = {"version": 1, "default_profile": "default", "profiles": {"default": {}}}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and existing.get("profiles"):
                    cfg = existing
                    cfg.setdefault("default_profile", "default")
                    cfg.setdefault("profiles", {}).setdefault("default", {})
            except json.JSONDecodeError:
                pass
        profile = cfg["profiles"].setdefault("default", {})
        if args.base_url:
            profile["base_url"] = args.base_url
        if args.api_key:
            profile["api_key"] = args.api_key
        if args.model:
            profile["model"] = args.model
        profile.setdefault("protocol", "auto")
        written = save_config_file(cfg)
        return emit(
            {
                "ok": True, "command": "config", "action": "init", "config_path": str(written),
                "profile": "default",
                "base_url": profile.get("base_url"),
                "api_key": mask_secret(profile.get("api_key")),
                "model": profile.get("model"),
                "permissions": "文件权限已设为 600（仅当前用户可读写）。",
                "next_step": "运行 imggen.py check 校验连通性。",
            },
            exit_code=EXIT_OK, command="config",
        )

    if action == "show":
        settings = resolve_settings(args)
        return emit(
            {
                "ok": True, "command": "config", "action": "show",
                "config_path": str(path), "exists": path.exists(),
                "profile": settings["profile"],
                "base_url": settings["base_url"],
                "api_key": mask_secret(settings["api_key"]),
                "api_key_keychain": settings["api_key_keychain"],
                "api_key_keychain_account": settings["api_key_keychain_account"],
                "model": settings["model"],
                "protocol": settings["protocol"],
                "timeout": settings["timeout"],
                "poll_interval": settings["poll_interval"],
                "poll_timeout": settings["poll_timeout"],
                "out_dir": settings["out_dir"],
                "download_with_auth": settings["download_with_auth"],
                "edit_transfer": settings["edit_transfer"],
                "default_size": settings["default_size"],
                "paths": {key: settings[key] for key in DEFAULT_PATHS},
                "async": redact(settings["async"]),
                "sources": settings["sources"],
            },
            exit_code=EXIT_OK, command="config",
        )

    if action == "set":
        if not args.assignments:
            raise ImgGenError(
                "缺少 key=value",
                stage="usage", exit_code=EXIT_USAGE,
                hint="例如：imggen.py config set base_url=https://host/v1 model=gpt-image-2 --profile main",
            )
        cfg = load_config_file()
        profile_name = args.profile or cfg.get("default_profile") or "default"
        cfg.setdefault("profiles", {}).setdefault(profile_name, {})
        profile = cfg["profiles"][profile_name]
        allowed = {
            "base_url", "api_key", "api_key_env", "api_key_file", "model", "protocol", "timeout",
            "poll_interval", "poll_timeout", "retries", "out_dir", "download_with_auth", "edit_transfer",
            "edit_image_field", "response_format", "models_path", "generation_path", "edit_path", "chat_path",
            "default_size", "api_key_keychain", "api_key_keychain_account",
        }
        allowed_async = set(DEFAULT_ASYNC.keys())
        changed = {}
        for item in args.assignments:
            if "=" not in item:
                raise ImgGenError(f"需要 key=value 形式：{item}", stage="usage", exit_code=EXIT_USAGE)
            key, value = item.split("=", 1)
            key, value = key.strip(), parse_scalar(value)
            if key == "default_profile":
                cfg["default_profile"] = value
            elif key.startswith("async."):
                sub = key.split(".", 1)[1]
                if sub not in allowed_async:
                    raise ImgGenError(f"不支持的 async 字段：{sub}", stage="usage", exit_code=EXIT_USAGE,
                                      hint=f"可用字段：{', '.join(sorted(allowed_async))}")
                profile.setdefault("async", {})[sub] = value
            elif key in allowed:
                profile[key] = value
            else:
                raise ImgGenError(f"不支持的配置项：{key}", stage="usage", exit_code=EXIT_USAGE,
                                  hint=f"可用字段：{', '.join(sorted(allowed))}，以及 async.<字段>")
            changed[key] = mask_secret(value) if SECRET_KEY_RE.search(key) else value
        written = save_config_file(cfg)
        return emit(
            {
                "ok": True, "command": "config", "action": "set", "config_path": str(written),
                "profile": profile_name, "changed": changed,
                "note": "密钥若通过命令行传入，可能残留在 shell 历史中；更安全的是用 api_key_env 指向环境变量。",
            },
            exit_code=EXIT_OK, command="config",
        )

    raise ImgGenError(f"未知的 config 子命令：{action}", stage="usage", exit_code=EXIT_USAGE)


# --------------------------------------------------------------------------- 入口


COMMANDS = {"check", "models", "gen", "edit", "batch", "config"}


def normalize_argv(argv):
    if not argv:
        return argv
    if argv[0] in ("-h", "--help", "--version"):
        return argv
    # config set 允许 key=value 与 --profile 任意顺序：把 --profile 提到位置参数之前
    if len(argv) >= 2 and argv[0] == "config" and argv[1] == "set":
        profile_args, rest, index = [], [], 2
        while index < len(argv):
            item = argv[index]
            if item == "--profile" and index + 1 < len(argv):
                profile_args = ["--profile", argv[index + 1]]
                index += 2
                continue
            if item.startswith("--profile="):
                profile_args = [item]
                index += 1
                continue
            rest.append(item)
            index += 1
        return ["config", "set"] + profile_args + rest
    if argv[0] not in COMMANDS and not argv[0].startswith("-"):
        return ["gen"] + argv
    return argv


def add_common_options(parser, *, with_model=True):
    group = parser.add_argument_group("配置")
    group.add_argument("--profile", help="使用配置文件里的哪个 profile（默认取 default_profile）")
    group.add_argument("--base-url", dest="base_url", help="接口根地址，例如 https://api.example.com/v1")
    group.add_argument("--api-key", dest="api_key", help="接口密钥（优先用环境变量/配置文件，避免进入 shell 历史）")
    group.add_argument("--api-key-keychain", dest="api_key_keychain", metavar="SERVICE",
                       help="从 macOS 钥匙串读取密钥（服务名，例如 ai-cmdme）")
    if with_model:
        group.add_argument("--model", help="模型名，例如 gpt-image-1、seedream-3.0、flux-pro")
    group.add_argument("--protocol", choices=["auto", "images", "chat", "async"],
                       help="请求协议（默认 auto：images → chat 回退）")
    group.add_argument("--timeout", type=float, help="单次 HTTP 请求超时秒数（默认 300；出图可能很慢）")
    group.add_argument("--poll-interval", dest="poll_interval", type=float, help="异步任务轮询间隔秒数（默认 3）")
    group.add_argument("--poll-timeout", dest="poll_timeout", type=float, help="异步任务总等待上限秒数（默认 900）")
    group.add_argument("--retries", type=int, help="失败重试次数（默认 1）")
    group.add_argument("--header", action="append", metavar="NAME:VALUE", help="附加请求头，可重复")
    group.add_argument("--param", action="append", metavar="KEY=VALUE",
                       help="附加请求体参数，可重复（如 seed=42、image_size=1024x1024）")
    group.add_argument("--extra-body", dest="extra_body", help="以 JSON 对象形式附加请求体字段")
    group.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（仅特殊网络环境使用）")
    group.add_argument("--debug", action="store_true", help="打印未捕获异常的堆栈，便于排查")
    output = parser.add_argument_group("输出")
    output.add_argument("--out-dir", dest="out_dir", help="图片保存目录（默认 ./imggen-out）")
    output.add_argument("--prefix", help="文件名前缀（默认取提示词片段）")
    output.add_argument("--format", choices=["auto", "png", "jpeg", "webp"],
                        help="目标格式；与返回格式不同时需要 Pillow")
    output.add_argument("--no-download", action="store_true", help="只返回图片 URL，不下载落盘")
    output.add_argument("--download-with-auth", dest="download_with_auth",
                        choices=["same-host", "always", "never"], help="下载图片时是否带鉴权头（默认 same-host）")
    output.add_argument("--max-bytes", dest="max_bytes", type=int, help="单张图片体积上限（默认 64MB）")
    output.add_argument("--dry-run", dest="dry_run", action="store_true", help="只构造请求并输出，不实际发送")
    output.add_argument("--quiet", action="store_true", help="不打印进度（仍输出 JSON 结果）")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="imggen.py",
        description="用自备的 OpenAI 兼容接口生成/编辑图片（文生图 / 图生图 / 图片编辑 / 批量）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 imggen.py check\n"
            "  python3 imggen.py gen \"一只戴墨镜的柴犬，写实摄影\" --size 1024x1024 -n 2\n"
            "  python3 imggen.py edit --image ./in.png --mask ./mask.png \"把背景换成雪天\"\n"
            "  python3 imggen.py batch --file jobs.jsonl --concurrency 2\n"
            "  python3 imggen.py config init --base-url https://api.example.com/v1 --api-key sk-... --model gpt-image-1\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"imggen.py {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="校验配置、连通性与模型列表")
    add_common_options(check, with_model=False)

    models = subparsers.add_parser("models", help="check 的别名：列出可用模型")
    add_common_options(models, with_model=False)

    gen = subparsers.add_parser("gen", help="文生图（带 --image 时为图生图/编辑）")
    gen.add_argument("prompt", nargs="?", help="图片描述（位置参数）")
    gen.add_argument("-p", "--prompt", dest="prompt_option", help="图片描述（等价于位置参数）")
    gen.add_argument("--prompt-file", dest="prompt_file", help="从文件读取提示词")
    gen.add_argument("-i", "--image", action="append", help="参考图路径或 URL，可重复（出现即走图生图/编辑）")
    gen.add_argument("--image-url", dest="image_url", action="append", help="参考图 URL（等价于 --image）")
    gen.add_argument("--mask", help="蒙版图片路径（局部重绘；仅 images 协议支持）")
    gen.add_argument("-n", "--count", type=int, help="生成数量（默认 1）")
    gen.add_argument("--size", help="尺寸，如 1024x1024、1536x1024、16:9 或 auto（以服务端支持为准）")
    add_common_options(gen)

    edit = subparsers.add_parser("edit", help="图生图 / 图片编辑 / 局部重绘")
    edit.add_argument("prompt", nargs="?", help="编辑要求或新图描述（位置参数）")
    edit.add_argument("-p", "--prompt", dest="prompt_option", help="编辑要求（等价于位置参数）")
    edit.add_argument("--prompt-file", dest="prompt_file", help="从文件读取提示词")
    edit.add_argument("-i", "--image", action="append", help="参考图路径或 URL，可重复（至少一张）")
    edit.add_argument("--image-url", dest="image_url", action="append", help="参考图 URL（等价于 --image）")
    edit.add_argument("--mask", help="蒙版图片路径（局部重绘；仅 images 协议支持）")
    edit.add_argument("-n", "--count", type=int, help="生成数量（默认 1）")
    edit.add_argument("--size", help="输出尺寸，如 1024x1024")
    add_common_options(edit)

    batch = subparsers.add_parser("batch", help="批量执行 JSONL 任务")
    batch.add_argument("--file", required=True, help="JSONL 任务文件，每行一个对象；用 - 表示从 stdin 读取")
    batch.add_argument("--concurrency", type=int, default=2, help="并发数（默认 2；出图较慢，别开太大）")
    batch.add_argument("--fail-fast", dest="fail_fast", action="store_true", help="遇到第一个失败就停止")
    add_common_options(batch)

    config = subparsers.add_parser("config", help="配置文件管理")
    config_sub = config.add_subparsers(dest="config_action")
    config_init = config_sub.add_parser("init", help="写入/更新配置文件（权限 600）")
    config_init.add_argument("--base-url", dest="base_url", help="接口根地址")
    config_init.add_argument("--api-key", dest="api_key", help="接口密钥")
    config_init.add_argument("--model", help="默认模型")
    config_init.add_argument("--force", action="store_true", help="已存在时覆盖")
    config_init.add_argument("--quiet", action="store_true")
    config_init.add_argument("--debug", action="store_true")
    config_show = config_sub.add_parser("show", help="显示解析后的配置（密钥打码）")
    add_common_options(config_show)
    config_sub.add_parser("path", help="显示配置文件路径")
    config_set = config_sub.add_parser("set", help="修改单个配置项")
    config_set.add_argument("assignments", nargs="*", help="key=value，可多个；支持 async.<字段>")
    config_set.add_argument("--profile", help="目标 profile")
    return parser


def main(argv=None):
    global _QUIET, _DEBUG
    argv = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    _QUIET = bool(getattr(args, "quiet", False))
    _DEBUG = bool(getattr(args, "debug", False))
    command = getattr(args, "command", None)
    if not command:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        if command in ("gen", "edit"):
            return cmd_generate(args, command=command)
        if command in ("check", "models"):
            return cmd_check(args)
        if command == "batch":
            return cmd_batch(args)
        if command == "config":
            return cmd_config(args)
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    except ImgGenError as error:
        return emit_error(command, error)
    except KeyboardInterrupt:
        return emit_error(command, ImgGenError("已被中断（KeyboardInterrupt）", stage="interrupt", exit_code=130))
    except Exception as error:  # 兜底：内部异常也以 JSON 反馈，方便 agent 转述
        return emit_error(
            command,
            ImgGenError(
                f"{type(error).__name__}: {error}",
                stage="internal",
                exit_code=EXIT_INTERNAL,
                hint="脚本内部错误，请把完整 JSON 输出反馈给维护者（或用 --debug 获取堆栈）。",
            ),
        )


if __name__ == "__main__":
    sys.exit(main())
