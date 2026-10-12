---
name: custom-imagegen
description: 用用户自备的 OpenAI 兼容 base_url + api_key 生成或编辑图片，默认文生图，同时支持图生图、图片编辑与局部重绘、批量任务、异步任务轮询。当用户提供自己的图片接口地址与密钥、或要求用 $custom-imagegen 出图、或需要等接口跑完并拿到落盘图片文件时使用。不用于内置 image_gen 工具的常规出图、矢量/代码绘图、以及纯提示词咨询。
metadata:
  short-description: 用自备接口出图（文生图/图生图/图片编辑）
---

# custom-imagegen（自备接口出图）

用用户自己的接口出图，等结果落地后再交付。核心脚本是 `scripts/imggen.py`：只用 Python 标准库，无需 pip 安装；默认文生图，带参考图即走图生图/编辑。

## 先决条件

- 需要用户提供 `base_url` 和 `api_key`。两者缺失时先向用户索要，不要猜测，也不要复用其他服务的密钥。
- macOS 上如果密钥已经存进钥匙串，脚本会自己读：本机默认映射写在 `<配置目录>/keychain_defaults.json`（形如 `{"用户名": "钥匙串服务名"}`），或直接用 `--api-key-keychain SERVICE` / profile 的 `api_key_keychain` 指定。
- 密钥只放环境变量或配置文件：不进仓库、不进日志、不回显完整值。脚本输出里密钥固定打码。
- 调用脚本用绝对路径，例如 `python3 <skill-dir>/scripts/imggen.py`。
- 需要 Python 3.9+（已在 3.9 与 3.12 实测）；只用标准库，不需要 pip 安装，也不需要联网装依赖。

## 首次配置

用户提供 `base_url` + `api_key` 后，任选一种方式配置，然后跑一次 `check` 校验：

```
python3 <skill-dir>/scripts/imggen.py config init --base-url https://api.example.com/v1 --api-key sk-... --model 模型名
python3 <skill-dir>/scripts/imggen.py check
```

不想写文件就设环境变量 `IMGGEN_BASE_URL` / `IMGGEN_API_KEY`。多服务商并存时用 `--profile` 切换，见 references/setup.md。

## 常用命令

```
check                                                      # 校验配置与连通性，列出疑似图片模型
gen "一只戴墨镜的柴犬，写实摄影" --size 1024x1024 -n 2      # 文生图（默认）
gen "改成赛博朋克风格" --image ./in.png                     # 参考图 -> 图生图/编辑
edit --image ./in.png --mask ./mask.png "把背景换成雪天"    # 局部重绘（mask 仅 images 协议支持）
batch --file jobs.jsonl --concurrency 2                     # 批量，每行一个 JSON 任务
```

常用参数：`--model`、`--size`、`-n`、`--protocol auto|images|chat|async`、`--param key=value`（透传非标准参数，如 `seed=42`、`image_size=1024x1024`）、`--out-dir`、`--dry-run`。

## 执行与等待

- 出图一般 5s~3min，异步任务更久。等待时给足超时（前台调用按 600s 级别设置），长时间没有输出不等于失败。
- 不要因为等待而重复提交同一个请求，可能重复扣费。需要长等待时用后台会话轮询脚本输出，而不是中断它。
- 参数拿不准先用 `--dry-run` 看请求体，再真正调用；`--quiet` 可只留 JSON 结果。

## 结果与反馈

- 判定成功看 JSON 的 `ok` 与 `images[].path`，并确认文件真实存在；不要只看命令是否返回。
- 交付时给出绝对路径、尺寸/格式、模型、协议与耗时；在聊天里用 Markdown 图片语法内联展示，例如 `![](/abs/path/img.png)`。多张图逐张展示。
- 失败时把 JSON 里的 `stage` / `error` / `http_status` / `hint` 转述给用户，并给出下一步动作，不要只说"失败了"。

## 输出位置

- 项目内使用的产物写进工作区目标目录，并在代码或文档里引用。
- 独立交付物写进本会话的 outputs 目录，回复时给绝对路径。
- 不覆盖已有文件；脚本发现重名会自动加 `-2` 后缀。

## 意图判断

- 无参考图：文生图（`gen`）。
- 给参考图要求换风格/换背景/保持主体：`edit --image`，多张参考图重复 `--image`。
- 只改局部：`edit --image ... --mask ...`。
- 一次要一批：`batch`，任务文件每行一个 JSON 对象。
- 服务端是"创建任务 + 查询结果"：profile 里设 `protocol: "async"`，配置见 references/protocols.md。

## 错误对照

| stage | 含义 | 处理 |
| --- | --- | --- |
| config | 缺 base_url / api_key / model | 让用户补齐，或先用 `check` 看模型列表 |
| network | 连不上、DNS、代理、证书 | 核对地址与网络；必要时 `--insecure`（不推荐） |
| api | 鉴权、参数、模型、端点问题 | 看 `hint`：改 `--size`/`--model`，或改 `--protocol chat` |
| task | 异步任务失败（多为内容策略或参数不支持） | 调整提示词后重试，不要原样重试 |
| poll | 异步轮询超时 / 路径不对 | 加大 `--poll-timeout`，或修 profile 的 `async.poll_paths` |
| output | 落盘失败、返回内容不是图片 | 换 `--out-dir`；确认模型确实支持出图 |

## 深入文档

- references/setup.md：配置文件结构、环境变量与优先级、多 profile、密钥安全。
- references/cli.md：全部命令与参数、JSON 输出结构、退出码、批量任务字段。
- references/protocols.md：images / chat / async 三种协议细节、异步任务字段配置、常见服务商适配与故障排查。
