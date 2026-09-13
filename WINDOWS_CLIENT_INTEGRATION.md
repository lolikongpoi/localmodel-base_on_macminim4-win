# Windows 客户端接入：Mac 本地图片 Tag 服务

## 目标

Windows 端应用需要把一张图片发送到局域网内 Mac 上的本地视觉模型服务，并取得结构化中文图片标签。图片只在局域网内传输；不调用云端 AI 服务。

## 连接信息

向服务维护者索取以下两项，**不要把 key 提交到 Git 或写进前端公开代码**：

| 项目 | 示例 | 说明 |
|---|---|---|
| 服务基地址 | `http://192.168.1.23:8787` | Mac 的局域网 IPv4 地址和端口；不要使用 `localhost` |
| API key | `由服务维护者单独提供` | 请求头 `X-API-Key` 的值 |

`localhost:8787` 只在 Mac 自己上才指向服务。在 Windows 上必须使用 Mac 的局域网 IP。

## 先做联通检查

PowerShell：

```powershell
$baseUrl = "http://MAC_LAN_IP:8787"
Invoke-RestMethod "$baseUrl/healthz"
```

预期得到：

```json
{
  "status": "ok",
  "model": "mlx-community/Qwen3-VL-4B-Instruct-4bit"
}
```

若失败，先确认：Mac 服务终端仍显示 `Uvicorn running on http://0.0.0.0:8787`、两台电脑在同一局域网、macOS 防火墙允许入站访问 8787、路由器没有开启无线客户端隔离。

## 图片识别接口

### 请求

```text
POST /v1/tag
Content-Type: multipart/form-data
X-API-Key: <API_KEY>
```

表单里必须有一个名为 `image` 的图片文件字段。接口接受常见图片 MIME 类型，默认最大上传大小为 20 MB。

### PowerShell 示例

```powershell
$baseUrl = "http://MAC_LAN_IP:8787"
$apiKey = "API_KEY"
$imagePath = "C:\\Images\\example.jpg"

$result = Invoke-RestMethod -Method Post `
  -Uri "$baseUrl/v1/tag" `
  -Headers @{ "X-API-Key" = $apiKey } `
  -Form @{ image = Get-Item $imagePath }

$result | ConvertTo-Json -Depth 8
```

### Python 示例

```python
import os
from pathlib import Path
import requests

base_url = os.environ["IMAGE_TAGGER_URL"].rstrip("/")
api_key = os.environ["IMAGE_TAGGER_API_KEY"]
image_path = Path(r"C:\\Images\\example.jpg")

with image_path.open("rb") as image_file:
    response = requests.post(
        f"{base_url}/v1/tag",
        headers={"X-API-Key": api_key},
        files={"image": (image_path.name, image_file, "image/jpeg")},
        timeout=180,
    )

response.raise_for_status()
result = response.json()
print(result)
```

使用环境变量的 PowerShell 启动方式：

```powershell
$env:IMAGE_TAGGER_URL = "http://MAC_LAN_IP:8787"
$env:IMAGE_TAGGER_API_KEY = "API_KEY"
python .\your_client.py
```

### 成功响应

```json
{
  "category": "anime_illustration",
  "tags": ["二次元插画", "蓝色长发", "校服", "室内", "半身像"],
  "character_candidates": [
    {"name": "角色官方名称", "confidence": 0.82}
  ],
  "contains_text": false,
  "summary": "一张室内光线下的二次元人物插画。",
  "model": "mlx-community/Qwen3-VL-8B-Instruct-4bit"
}
```

字段定义：

| 字段 | 类型 | 用法 |
|---|---|---|
| `category` | string | 图片主类别，见下方固定枚举 |
| `tags` | string[] | 5–20 个中文描述 tag；适合展示、检索和初步归类 |
| `character_candidates` | object[] | 可选的二次元角色候选，包含 `name` 和 0–1 的 `confidence` |
| `contains_text` | boolean | 图片中是否存在可见文字 |
| `summary` | string | 简短中文摘要 |
| `model` | string | 实际完成本次推理的本地模型 ID |

`category` 固定为以下之一：

```text
anime_illustration
chat_screenshot
meme_or_sticker
casual_photo
portrait_photo
professional_photography
wallpaper_or_desktop
document_or_ui
other
```

角色识别是候选结果，不应当当作事实。建议调用端仅在 `confidence >= 0.80` 时自动采用角色名称；低于该阈值时用于提示、人工确认或仅保留通用 tags。

## 模型查询与切换

当前加载的模型和可选预设：

```text
GET /v1/models
X-API-Key: <API_KEY>
```

返回：

```json
{
  "current_model": "mlx-community/Qwen3-VL-4B-Instruct-4bit",
  "presets": {
    "4b": "mlx-community/Qwen3-VL-4B-Instruct-4bit",
    "8b": "mlx-community/Qwen3-VL-8B-Instruct-4bit"
  }
}
```

切换模型（需要 API key）：

```text
POST /v1/model
Content-Type: application/json
X-API-Key: <API_KEY>

{"preset":"4b"}
```

或：

```json
{"preset":"8b"}
```

模型切换会暂停所有待处理的识别请求，卸载当前模型后加载目标模型。首次使用 8B 可能下载约 5.8GB，耗时取决于网络；调用端应给这条请求至少 10 分钟超时，并在此期间禁用提交按钮。普通图片识别建议设为 180 秒超时。不要在每次识别前切换模型。

## 错误处理与重试

| HTTP 状态 | 含义 | 客户端处理 |
|---|---|---|
| 200 | 成功 | 解析 JSON |
| 400 | 空文件等无效请求 | 提示用户更换图片，不重试 |
| 401 | API key 不正确 | 停止调用，要求重新配置 key |
| 413 | 图片超过大小限制 | 压缩/缩小图片后重试 |
| 415 | 不是可读图片 | 更换图片格式 |
| 502 | 本地模型推理或输出格式异常 | 最多重试一次；仍失败则记录服务返回文本并检查 Mac 终端日志 |
| 网络超时/连接失败 | 服务未启动、防火墙或局域网问题 | 先调用 `/healthz`，不要对同一图片无限重试 |

服务在 16 GB Mac 上会把推理请求排队执行；客户端需要允许单张图片处理耗时数十秒，尤其是 8B 和较大截图。建议一次只发送一张图，最多做 1 次短暂退避重试。

## 浏览器与安全限制

- 不能双击打开 `web/index.html`；应通过 `http://MAC_LAN_IP:8787/` 访问内置测试页。
- 如果 Windows 应用是浏览器页面，且页面不在这个 Mac 服务的同源地址下，浏览器会触发 CORS 限制。优先让 Windows 应用的后端/桌面客户端调用本接口，再把结果传给前端；不要把 API key 直接嵌入公开网页。
- 这是 HTTP 局域网服务。不要在路由器上将 8787 映射到公网；API key 应通过安全渠道传递，并可随时在 Mac 上重新生成。

## 可直接交给 Windows Codex 的任务说明

```text
请在当前 Windows 项目中集成一个“局域网图片 Tag 服务”客户端。

服务：IMAGE_TAGGER_URL 环境变量，例如 http://192.168.1.23:8787
密钥：IMAGE_TAGGER_API_KEY 环境变量；不得写死、提交到 Git 或暴露给浏览器前端。

实现要求：
1. 用 multipart/form-data POST ${IMAGE_TAGGER_URL}/v1/tag，图片字段名必须为 image，请求头为 X-API-Key。
2. 请求超时设为 180 秒；正确区分 401、413、415、502 和网络异常，并给出用户可理解的错误信息。
3. 解析 JSON 的 category、tags、character_candidates、contains_text、summary、model 字段；角色只在 confidence >= 0.80 时自动采用，其余仅展示为候选。
4. 在提交图片前或配置页提供 GET /healthz 联通检查。
5. 若实现模型选择，调用 GET /v1/models 和 POST /v1/model（body: {"preset":"4b"} 或 {"preset":"8b"}）。切换请求给 10 分钟超时，并禁用并发识别。
6. 若是 Web 前端，不要直接跨域调用并暴露 key；请经由项目后端代理，或说明需要服务端另行配置 CORS。
7. 先给出你将修改的文件与调用路径，然后实施并提供一个可验证的本地测试方式。
```
