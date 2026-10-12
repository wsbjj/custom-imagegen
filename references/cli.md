# 命令行参考

脚本：`<skill-dir>/scripts/imggen.py`，用法 `python3 imggen.py <命令> [参数]`。

省略子命令时，第一个非选项参数会当作 `gen` 的提示词，例如 `imggen.py "一只猫"` 等价于 `imggen.py gen "一只猫"`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `check` | 校验配置与连通性（GET `/models`），列出疑似图片模型；HTTP 401/403 直接报错 |
| `models` | `check` 的别名 |
| `gen` | 文生图；带 `--image` 时自动走图生图/编辑 |
| `edit` | 图生图 / 图片编辑 / 局部重绘（至少一张 `--image`） |
| `batch` | 按 JSONL 文件批量执行 |
| `config` | `init` / `show` / `path` / `set` |

## 通用参数

配置类：`--profile`、`--base-url`、`--api-key`、`--api-key-keychain SERVICE`（macOS 钥匙串）、`--model`、`--protocol auto|images|chat|async`、`--timeout`、`--poll-interval`、`--poll-timeout`、`--retries`、`--header NAME:VALUE`（可重复）、`--param KEY=VALUE`（可重复）、`--extra-body JSON`、`--insecure`、`--debug`。

输出类：`--out-dir`、`--prefix`、`--format auto|png|jpeg|webp`、`--no-download`、`--download-with-auth same-host|always|never`、`--max-bytes`、`--dry-run`、`--quiet`。

`--param` 的值会按 JSON 规则解析：`seed=42` 是数字，`watermark=false` 是布尔，`modalities='["image","text"]'` 是数组。

## gen

```bash
python3 imggen.py gen "一只在雪地里的柴犬，写实摄影" --size 1024x1024 -n 2 --out-dir ./out
python3 imggen.py gen --prompt-file prompt.txt --model flux-pro   # 长提示词
python3 imggen.py gen "把背景换成雪天" --image ./in.png           # 参考图 => 编辑端点
```

参数：`prompt`（位置参数，或用 `-p/--prompt`）、`--prompt-file`、`-i/--image`（可重复）、`--image-url`、`--mask`、`-n/--count`、`--size`。

## edit

```bash
python3 imggen.py edit --image ./in.png "只改背景为雪天，主体保持不变"
python3 imggen.py edit --image a.png --image b.png "把两张图的人物合成到同一场景"
python3 imggen.py edit --image ./in.png --mask ./mask.png "把被遮住的区域换成花海"
```

`--mask` 只在 images 协议下作为表单字段上传；chat 协议会给出警告并忽略蒙版。

## batch

```bash
python3 imggen.py batch --file jobs.jsonl --concurrency 2 [--fail-fast] [--dry-run]
cat jobs.jsonl | python3 imggen.py batch --file -
```

每行一个 JSON 对象，支持字段：`prompt`（或 `text`）、`image`、`images`、`mask`、`count`（或 `n`）、`size`、`model`、`params`、`prefix`、`out_dir`、`format`、`download`。以 `#` 开头的行和空行会被跳过。

并发默认 2；出图接口通常限流，别开太大。任意任务失败时整批退出码为 4，但其余任务仍会完成并逐个给出结果。

## config

```bash
python3 imggen.py config init --base-url https://api.example.com/v1 --api-key sk-... --model gpt-image-1 [--force]
python3 imggen.py config path
python3 imggen.py config show [--profile relay]
python3 imggen.py config set --profile relay model=seedream-3.0 retries=2 timeout=600
python3 imggen.py config set --profile relay async.poll_paths='["/v1/images/tasks/{task_id}"]'
```

`config init` 在文件已存在时不会覆盖（退出码 2），需要显式 `--force`；只想改单项用 `config set`。

## 成功输出

```json
{
  "ok": true,
  "command": "gen",
  "profile": "default",
  "protocol": "images",
  "model": "gpt-image-1",
  "prompt": "一只在雪地里的柴犬",
  "elapsed_seconds": 12.4,
  "request": { "url": "https://.../v1/images/generations", "method": "POST", "payload": { "...": "已掩码" }, "usage": {} },
  "images": [
    { "path": "/abs/out/xxx-1.png", "url": null, "source": "data.0.b64_json",
      "index": 1, "bytes": 123456, "width": 1024, "height": 1024, "format": "png" }
  ],
  "warnings": []
}
```

`--no-download` 时 `images[].path` 为 `null`，只保留 `url`。

## 失败输出

```json
{
  "ok": false,
  "command": "gen",
  "stage": "api",
  "error": "HTTP 400：invalid size",
  "exit_code": 4,
  "http_status": 400,
  "hint": "请求参数被拒绝：换一个 size/模型……",
  "response_excerpt": "{ ... }",
  "url": "https://.../v1/images/generations"
}
```

失败信息同时输出到标准输出（JSON）与标准错误（人读摘要）。标准错误里的进度行以 `[imggen]` 开头。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 脚本内部错误（请把完整 JSON 反馈给维护者） |
| 2 | 配置错误：缺 base_url / api_key / model，或配置文件不合法、已存在未加 --force |
| 3 | 网络错误：连不上、DNS、代理、证书，或下载图片失败 |
| 4 | API/HTTP 错误：参数、鉴权、模型不存在、返回非 JSON、返回里没有图片 |
| 5 | 异步任务失败（服务端返回 failed 一类状态） |
| 6 | 异步轮询超时 |
| 7 | 写文件失败，或返回数据不是可识别的图片格式 |
| 8 | 用法错误：缺提示词、参数格式不对、参考图不存在 |
| 130 | 被 Ctrl-C 中断 |

## 行为细节

- 输出文件名 `<前缀>-<时间戳>-<序号>.<扩展名>`，前缀默认取提示词片段；重名自动加 `-2`。
- `--format` 与返回格式不同时用 Pillow 转换；没装 Pillow 会保留原格式并写入 `warnings`。
- 尺寸/格式解析不依赖 Pillow：png / jpeg / gif / webp / bmp 都能读宽高。
- 下载图片时默认只对同主机域名带 `Authorization`；CDN 在别的域名却发现 401/403 时，用 `--download-with-auth always`。
- `--retries` 对 429/502/503/504 与连接类错误生效；POST 出图重试可能重复计费，介意就设 0。
- macOS 上如果没声明任何密钥来源，会自动尝试读钥匙串：本机默认映射放在 `<配置目录>/keychain_defaults.json`（形如 `{"用户名": "钥匙串服务名"}`），也可用 `IMGGEN_KEYCHAIN_DEFAULTS` 传 JSON。用 `--api-key-keychain` 或 profile 的 `api_key_keychain` 可显式指定；一旦声明了来源但读不到，会直接报错退出码 2。
