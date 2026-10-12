# 协议细节与适配

`--protocol` 决定请求怎么发。默认 `auto`：先按 images 发，遇到"端点不存在"类错误再回退 chat。

| 协议 | 请求 | 适用 |
| --- | --- | --- |
| `images` | `POST /images/generations` 或 `/images/edits` | 标准 OpenAI 兼容图片接口、多数中转站 |
| `chat` | `POST /chat/completions` | 对话式出图模型（nano-banana、gemini-image、qwen-image 等） |
| `async` | 创建任务 + 轮询查询接口 | 即梦/火山、通义、部分中转站的异步绘图 |

## auto 的回退规则

- 无参考图 → 先 `/images/generations`，失败则 `/chat/completions`。
- 有参考图或蒙版 → 先 `/images/edits`，失败再试 chat（chat 无蒙版能力）。
- 只有"端点不存在"类错误才回退：HTTP 404/405，或 400 且消息里含 not found / unsupported / unknown endpoint / 不支持 / 未找到 等。鉴权失败、参数错误、余额不足不会盲目换协议，避免掩盖真实原因。
- 回退成功时 `warnings` 里会说明"images 协议不可用，已自动回退"。

## images 协议

文生图请求体（未列出的字段用 `--param` / `extra_body` 追加）：

```json
{ "model": "gpt-image-1", "prompt": "...", "n": 2, "size": "1024x1024" }
```

- `n` 只在大于 1 时发送；`size` 原样透传（不同服务支持 1024x1024、1536x1024、16:9、auto 等，按服务端为准）。
- `response_format` 默认不发送；服务端要求时用 profile 的 `response_format` 设为 `b64_json` 或 `url`。

图片编辑（图生图 / 局部重绘）：

- `edit_transfer: "multipart"`（默认）：multipart 表单，单图字段名 `image`，多图 `image[]`，蒙版字段 `mask`；字段名不对时用 `edit_image_field` 覆盖。
- `edit_transfer: "base64"`：JSON 请求体，`image` 为 `data:image/png;base64,...`（多图时为数组），`mask` 同理。用于只接受 JSON 的中转。
- 参考图支持本地路径与 http(s) URL；URL 会先由脚本下载再上传。

响应解析（三种协议共用）：脚本递归扫描响应，认这些位置：

- `b64_json` / `base64` / `b64` / `image_base64` / `image_data` 等字段里的 base64；
- `url` / `image_url` / `img_url` / `image` / `images` 里的 http(s) 链接；
- 任意字符串里的 `data:image/...;base64,...`、Markdown `![alt](url)`；
- 没有明确字段名时长 base64 文本，只有真能解码成图片才会被采纳。

落盘前会校验文件头（png/jpeg/gif/webp/bmp/avif），不是图片就跳过；若整个响应没有任何可用图片，则报"接口返回成功，但没有找到图片数据"。

## chat 协议

无参考图时 `content` 是字符串；有参考图时是 parts 数组（text + `image_url`，图片以内联 data URI 传入，避免服务端拉不到外链）。

对话式模型常见的额外要求，用 `--param` 补：

```bash
python3 imggen.py gen "画一只猫" --protocol chat --model gemini-2.5-flash-image \
  --param 'modalities=["image","text"]'
```

解析范围包含 `choices[].message.content`（Markdown 图片、裸 URL、data URI）、`choices[].message.images[]`，以及整个响应里的通用图片字段。只返回文字时报错并提示检查模型是否支持出图。

## async 协议

流程：创建任务 → 取 `task_id` → 轮询直到成功/失败/超时 → 从结果里提取图片。

```json
{
  "protocol": "async",
  "async": {
    "create_path": "/images/generations",
    "poll_paths": ["/images/tasks/{task_id}"],
    "poll_method": "GET",
    "task_id_paths": ["task_id", "data.task_id", "output.task_id"],
    "status_paths": ["status", "data.status", "data.task_status"],
    "progress_paths": ["progress", "data.progress"],
    "success_values": ["succeeded", "success", "completed", "done"],
    "failure_values": ["failed", "canceled", "expired"],
    "extra_body": {}
  }
}
```

要点：

- `task_id_paths` / `status_paths` / `progress_paths` 用点号路径，支持数组下标（`data.items.0.id`）。默认值覆盖了常见命名；找不到 task_id 时会自动搜 `*_id` 形式键名。
- `poll_paths` 支持多个候选（用 `{task_id}` 占位），依次尝试，404 会自动换下一个；全部 404 会报错并列出尝试过的路径。
- `poll_method` 默认 GET；服务端要求 POST 查询时改成 `POST@@。
- 轮询过程中每轮向标准错误打印 `[imggen] 轮询 #n：status=... progress=... 已等待 Ns`，便于观察进度，不影响标准输出的 JSON。
- 创建接口若直接返回了图片，会跳过轮询直接落盘（`request.note` 说明）。
- 超时（`--poll-timeout`）退出码 6；服务端报 failed 退出码 5；两者都会带上 `task_id` 与最后状态。
- 异步协议支持参考图：创建请求里 `image` / `mask` 以 data URI 传递。

用户常见说法与处理：用户说"用即梦/火山/通义的异步接口"时，先问清创建与查询路径（或先试默认路径），再写进 profile 的 `async` 字段，用 `--dry-run` 核对请求体。

## 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 401 / 403 | 密钥错、未开通模型、地区限制 | 核对 `api_key` 与 `base_url` 是否同一服务；控制台确认模型权限 |
| 404 且提示 model not found | 模型名不对，或该模型不在这个端点 | `check` 看模型列表；换 `--model`；试 `--protocol chat` |
| 404 且提示 path/route | base_url 少了 `/v1`，或端点路径不同 | 补全 base_url 前缀，或改 profile 的 `generation_path` 等 |
| 400/422 参数错误 | size/参数名不匹配 | 去掉可疑 `--param`；部分服务用 `image_size` 而非 `size` |
| 429 | 限流或余额不足 | 降低并发、稍后重试、检查额度 |
| 返回非 JSON | 地址指向了网页 | 确认 `base_url` 是 API 根路径 |
| 成功但没图片 | 模型不支持出图 / 需要 modalities / 结构与默认不同 | 换模型；加 `--param 'modalities=["image","text"]'`；或把响应结构反馈给维护者 |
| 返回的不是图片 | 返回了文本或错误信息 | 看 `response_excerpt`；确认模型是图片模型 |
| 图片链接下载 401 | CDN 需要鉴权 | `--download-with-auth always` |
| 轮询一直 running | 服务端排队慢 | 加大 `--poll-timeout`；确认 task_id 对应查询接口 |
| 超时但任务可能已完成 | 查询路径不对 | 修 `async.poll_paths`，再按 task_id 查询 |
