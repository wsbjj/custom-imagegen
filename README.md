# custom-imagegen

给 AI agent 客户端（Codex、Claude Code 等）用的出图 skill：用你自己提供的 OpenAI 兼容 `base_url` + `api_key` 生成或编辑图片，默认文生图。

## 能力

- 文生图（默认）、图生图、图片编辑与局部重绘（`--mask`）
- 批量任务（JSONL）与异步任务（创建 + 轮询）
- 协议自动回退：`images` → `chat`，覆盖标准图片接口与对话式出图模型
- 只用 Python 标准库（3.9+ 实测），零 pip 依赖
- 结果落到本地文件；失败时输出结构化 JSON（`stage` / `error` / `hint` / 退出码），便于 agent 直接转述

## 安装

把本目录复制到客户端的 skills 目录：

```bash
# Codex
rsync -a ./ ~/.codex/skills/custom-imagegen/
# Claude Code
rsync -a ./ ~/.claude/skills/custom-imagegen/
```

## 快速开始

```bash
python3 scripts/imggen.py config init --base-url https://your-api/v1 --api-key sk-... --model gpt-image-2
python3 scripts/imggen.py check
python3 scripts/imggen.py gen "一只戴墨镜的柴犬，写实摄影"
python3 scripts/imggen.py edit --image in.png --mask mask.png "把背景换成雪天"
python3 scripts/imggen.py batch --file jobs.jsonl
```

## 密钥来源

按优先级：`--api-key` / `--api-key-keychain` > `IMGGEN_API_KEY` > profile 的 `api_key` / `api_key_env` / `api_key_keychain` / `api_key_file`；未声明任何来源时才会使用本机钥匙串默认映射（`<配置目录>/keychain_defaults.json`）与 `OPENAI_API_KEY`。macOS 上推荐把密钥放进钥匙串：

```bash
security add-generic-password -U -a "$USER" -s your-service -w
python3 scripts/imggen.py config set api_key_keychain=your-service
```

脚本输出里密钥一律打码；仓库中不含任何密钥或个人标识。

## 文档

- `SKILL.md`：给 agent 的执行规则（何时用、怎么等、怎么回报错误）
- `references/setup.md`：配置结构、环境变量、密钥安全
- `references/cli.md`：命令、参数、退出码、JSON 输出
- `references/protocols.md`：三种协议与异步任务配置
