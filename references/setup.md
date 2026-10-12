# 配置与密钥

## 配置文件位置

按优先级解析：

1. `IMGGEN_CONFIG` 指定的文件路径；
2. `IMGGEN_CONFIG_DIR` 指定的目录下的 `config.json`；
3. `$XDG_CONFIG_HOME/imggen/config.json`；
4. 默认 `~/.config/imggen/config.json`。

`config init` / `config set` 写文件时会把权限设为 `600`（仅当前用户可读写）。密钥放在这里比放进命令历史更安全。

## 文件结构

```json
{
  "version": 1,
  "default_profile": "default",
  "profiles": {
    "default": {
      "base_url": "https://api.example.com/v1",
      "api_key_env": "MY_IMAGE_API_KEY",
      "model": "gpt-image-1",
      "protocol": "auto",
      "timeout": 300,
      "poll_interval": 3,
      "poll_timeout": 900,
      "retries": 1,
      "out_dir": "imggen-out",
      "download_with_auth": "same-host",
      "edit_transfer": "multipart",
      "edit_image_field": null,
      "response_format": "auto",
      "headers": { "X-Custom": "value" },
      "extra_body": { "quality": "high" },
      "models_path": "/models",
      "generation_path": "/images/generations",
      "edit_path": "/images/edits",
      "chat_path": "/chat/completions",
      "async": { }
    }
  }
}
```

字段含义：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | 无 | 接口根地址，通常包含 `/v1`；结尾斜杠会被去掉 |
| `api_key` | 无 | 密钥；与 `api_key_env`、`api_key_file` 三选一 |
| `api_key_env` | 无 | 存放密钥的环境变量名（推荐，避免把密钥写进文件） |
| `api_key_keychain` | 无 | macOS 钥匙串的服务名（`security` 的 `-s`），比密钥文件更安全 |
| `api_key_keychain_account` | 当前用户 | 钥匙串条目的 account（`-a`），一般不用填 |
| `api_key_file` | 无 | 存放密钥的文件路径，读取后会 strip |
| `model` | 无 | 默认模型；命令行 `--model` 可覆盖 |
| `protocol` | `auto` | `auto` / `images` / `chat` / `async` |
| `timeout` | 300 | 单次 HTTP 请求超时（秒）；出图慢的服务可调大 |
| `poll_interval` / `poll_timeout` | 3 / 900 | 异步任务轮询间隔与总等待上限（秒） |
| `retries` | 1 | 429/5xx/连接类错误的重试次数（POST 出图重试可能重复计费，介意就设 0） |
| `out_dir` | `imggen-out` | 图片保存目录（相对当前工作目录） |
| `download_with_auth` | `same-host` | 下载图片链接是否带鉴权头：`same-host` / `always` / `never` |
| `edit_transfer` | `multipart` | 编辑请求的传输方式：`multipart` / `base64` |
| `edit_image_field` | 自动 | 覆盖编辑请求里的图片字段名（默认单图为 `image`，多图 `image[]`） |
| `response_format` | `auto` | `auto` 时不发送该字段；可设 `b64_json` 或 `url` |
| `default_size` | 空 | 未传 `--size` 时使用的默认尺寸（例如被限额在 1K 的 key 就设 `1024x1024`） |
| `headers` | @@BT@{}` | 附加请求头，与 `--header` 合并 |
| `extra_body` | @@BT@{}` | 附加请求体字段，与 `--param` 合并 |
| `models_path` 等 | 见上 | 端点路径，按服务端实际路径覆盖 |
| `async` | 见 protocols.md | 异步任务的创建/轮询/字段路径配置 |

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `IMGGEN_BASE_URL` / `IMGGEN_API_KEY` / `IMGGEN_MODEL` | 覆盖 profile 对应字段 |
| `IMGGEN_PROFILE` | 选择 profile |
| `IMGGEN_PROTOCOL` | 覆盖协议 |
| `IMGGEN_TIMEOUT` / `IMGGEN_POLL_INTERVAL` / `IMGGEN_POLL_TIMEOUT` / `IMGGEN_RETRIES` | 覆盖超时与重试 |
| `IMGGEN_OUT_DIR` | 覆盖输出目录 |
| `IMGGEN_CONFIG` / `IMGGEN_CONFIG_DIR` | 指定配置文件或配置目录 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 兜底（IMGGEN_* 未设置时才会用到） |

取值优先级：命令行参数 > `IMGGEN_*` 环境变量 > `OPENAI_*` 环境变量 > profile 配置 > 内置默认值。

密钥解析顺序：`--api-key` > `--api-key-keychain` > `IMGGEN_API_KEY` > profile.api_key > profile.api_key_env > profile.api_key_keychain > profile.api_key_file。

只有在**完全没有声明任何密钥来源**时，才会启用两个隐式来源：macOS 本机钥匙串默认（见下）→ `OPENAI_API_KEY`。也就是说，写错钥匙串服务名或环境变量名会直接报「缺少 api_key」并退出码 2，不会悄悄改用别的密钥。

## macOS 钥匙串（推荐的单机方案）

把密钥存进系统钥匙串，配置文件里只写服务名：

```bash
security add-generic-password -U -a "$USER" -s ai-cmdme -w      # -w 放最后会提示输入，不进 shell 历史
python3 imggen.py config set --profile cmdme api_key_keychain=ai-cmdme
python3 imggen.py check --profile cmdme                          # 输出里 sources.api_key 会显示 config:keychain
```

直接命令行指定也行：`imggen.py check --api-key-keychain ai-cmdme`。

本机默认映射：在配置目录放一个 `keychain_defaults.json`（形如 `{"你的用户名": "钥匙串服务名"}`），或在环境变量 `IMGGEN_KEYCHAIN_DEFAULTS` 里传同样的 JSON。未声明任何密钥来源时，脚本会用当前用户名去查这张表，命中就自动读取对应钥匙串条目；仓库里的内置表默认是空的，不含任何个人标识。

细节：读取用 `security find-generic-password`（先带 account，再只按 service），失败后回退 `find-internet-password`；单次调用 10 秒超时，如果钥匙串弹授权框而没人点，会超时并继续找其他来源。可用 `IMGGEN_SECURITY_BIN` 覆盖 `security` 路径（仅供测试）。

## 多服务商

用 profile 并存，调用时 `--profile 名字` 选择：

```text
config set --profile relay base_url=https://relay.example.com/v1 model=flux-pro api_key_env=RELAY_KEY
config set --profile official base_url=https://api.example.com/v1 model=gpt-image-1 api_key_env=OFFICIAL_KEY
config set default_profile=relay
```

常见差异（改路径或加参数即可适配）：

- 用 `image_size` 而不是 `size`：`config set --profile p extra_body='{"image_size": "1024x1024"}'`，或调用时 `--param image_size=1024x1024`。
- 对话式出图（nano-banana 一类）：设 `protocol: "chat"`。
- 创建任务 + 查询结果：设 `protocol: "async"` 并配置 `async` 字段，见 protocols.md。

## 密钥安全

- 不要用 `--api-key` 把密钥写进 shell 历史；优先 `api_key_env`。
- 用户如果在聊天里直接贴了密钥，按用户意愿完成配置，但要提醒他轮换或改用环境变量。
- 脚本输出的 JSON 里 `api_key` 一律打码；请求体中的密钥字段在 meta 中也会被掩码。
- 不要把配置文件提交进仓库（`~/.config` 之外的位置尤其注意）。
