# custom-imagegen

> 用你自己提供的 OpenAI 兼容接口出图：文生图（默认）、图生图、图片编辑。

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![Protocols](https://img.shields.io/badge/protocols-images%20%7C%20chat%20%7C%20async-blue)

给 AI agent 客户端（Codex、Claude Code、WorkBuddy 等）使用的出图 skill。不需要任何第三方 Python 包，也不需要把密钥存成明文文件——macOS 上可以直接读系统钥匙串。

![演示：由本 skill 生成的图片](assets/demo-corgi.jpg)

## 它做什么

- **文生图**：`gen "一只戴墨镜的柴犬，写实摄影"`（默认模式）
- **图生图 / 图片编辑**：`edit --image in.png "把背景换成雪天"`，可传多张参考图
- **局部重绘**：加 `--mask mask.png` 只改指定区域
- **批量任务**：`batch --file jobs.jsonl --concurrency 2`
- **异步任务**：创建 + 轮询，适配"提交任务再查结果"的服务（即梦/火山、通义等）
- **协议自适应**：`images`（标准图片接口）优先，端点不存在时自动回退 `chat`（对话式出图模型）
- **结构化反馈**：标准输出永远是 JSON，失败时带 `stage` / `error` / `http_status` / `hint` 与退出码，agent 可以直接转述给用户

## 安装

复制到客户端的 skills 目录即可：

```bash
git clone https://github.com/wsbjj/custom-imagegen.git /tmp/custom-imagegen
# Codex
rsync -a /tmp/custom-imagegen/ ~/.codex/skills/custom-imagegen/
# Claude Code
rsync -a /tmp/custom-imagegen/ ~/.claude/skills/custom-imagegen/
```

卸载就是把对应目录删掉。

## 快速开始

```bash
# 1) 写入配置（base_url 通常以 /v1 结尾）
python3 scripts/imggen.py config init --base-url https://your-api.example.com/v1 --api-key sk-... --model gpt-image-2

# 2) 校验连通性与可用模型
python3 scripts/imggen.py check

# 3) 出图
python3 scripts/imggen.py gen "一只戴墨镜的柴犬，写实摄影" --size 1024x1024
python3 scripts/imggen.py edit --image ./in.png --mask ./mask.png "把背景换成雪天"
python3 scripts/imggen.py batch --file jobs.jsonl --concurrency 2
```

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `check` | 校验配置与连通性，列出疑似图片模型 |
| `gen` | 文生图；带 `--image` 时走图生图/编辑 |
| `edit` | 图生图 / 图片编辑 / 局部重绘 |
| `batch` | 按 JSONL 批量执行 |
| `config` | `init` / `show` / `path` / `set` 配置管理 |

常用参数：`--model`、`--size`、`-n`、`--protocol auto|images|chat|async`、`--param key=value`（透传非标准参数，如 `seed=42`、`image_size=1024x1024`）、`--out-dir`、`--dry-run`。完整列表见 [references/cli.md](references/cli.md)。

## 密钥怎么放

按优先级查找，任何一个命中即使用：

1. `--api-key` / `--api-key-keychain SERVICE`
2. 环境变量 `IMGGEN_API_KEY`
3. profile 的 `api_key` / `api_key_env` / `api_key_keychain` / `api_key_file`

只有完全没有声明来源时，才会使用本机钥匙串默认映射（`<配置目录>/keychain_defaults.json`，形如 `{"用户名": "钥匙串服务名"}`）与 `OPENAI_API_KEY`。声明了来源却读不到会直接报错，不会静默换用别的密钥。

macOS 推荐把密钥放进钥匙串：

```bash
security add-generic-password -U -a "$USER" -s your-service -w     # -w 放最后会提示输入，不进 shell 历史
python3 scripts/imggen.py config set api_key_keychain=your-service
```

密钥不会出现在日志、请求回显或本仓库里，输出中一律打码。更多配置项见 [references/setup.md](references/setup.md)。

## 协议

| 协议 | 请求 | 适用 |
| --- | --- | --- |
| `images` | `/images/generations`、`/images/edits` | 标准 OpenAI 兼容图片接口、多数中转站 |
| `chat` | `/chat/completions` | 对话式出图模型（nano-banana、qwen-image 等） |
| `async` | 创建任务 + 轮询查询接口 | 异步绘图服务（路径与状态字段可配） |

默认 `auto`：先试 `images`，只在"端点不存在"类错误时才回退 `chat`，鉴权与参数错误不会被掩盖。异步任务的字段配置见 [references/protocols.md](references/protocols.md)。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 401 / 403 | 核对 `api_key` 与 `base_url` 是否同一服务、模型是否已开通 |
| 404 且提示 model not found | 用 `check` 看模型列表，或试 `--protocol chat` |
| 404 且提示 path | `base_url` 通常需要包含 `/v1` |
| 400 / 422 参数错误 | 去掉可疑的 `--param`；部分服务用 `image_size` 而非 `size` |
| 429 | 限流或额度不足：稍后重试，或降低 `--concurrency` |
| 成功但没图片 | 确认模型支持出图，或加 `--param 'modalities=["image","text"]'` |
| 异步一直 running | 加大 `--poll-timeout`，并确认 `async.poll_paths` 正确 |

## 目录结构

```text
custom-imagegen/
├── SKILL.md              # 给 agent 的执行规则（何时用、怎么等、怎么回报错误）
├── agents/openai.yaml    # 客户端 UI 元数据
├── scripts/imggen.py     # 零依赖 CLI（仅 Python 标准库）
└── references/           # setup / cli / protocols 三份深入文档
```

## 环境要求

Python 3.9+（已在 3.9 与 3.12 实测），无需 pip 安装任何包。macOS、Linux、Windows 都能跑；钥匙串相关功能仅 macOS。

## English

`custom-imagegen` is a drop-in skill for AI coding agents (Codex, Claude Code, …). It generates and edits images through **your own** OpenAI-compatible endpoint: text-to-image by default, plus image-to-image, masked editing, batch jobs and async task polling. Pure standard-library Python (3.9+), structured JSON output with actionable error hints, and macOS Keychain support for the API key. See [SKILL.md](SKILL.md) and [references/](references) for details.
