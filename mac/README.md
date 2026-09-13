# 本地局域网图片 Tag 服务

面向 Apple Silicon Mac 的私有图片识别服务。Windows 电脑通过局域网上传一张图片，服务返回中文 tag、图片类别、简短摘要，以及**仅在模型有足够把握时**返回的二次元角色候选。

支持常见 JPEG、PNG、WebP 等格式，也支持 iPhone/iPad 常见的 HEIC/HEIF 照片；服务会在本机解码并转换后再推理。

## 这台 Mac 的推荐配置

- 模型：`mlx-community/Qwen3-VL-4B-Instruct-4bit`（约 3.1 GB）
- 推理框架：MLX，专为 Apple Silicon 的统一内存设计
- 运行方式：单进程、请求排队；适合一台或少量 Windows 电脑调用

这台 M4 / 16 GB Mac 可以运行这个配置。它对聊天截图、表情包、随手拍、人物照、摄影作品、桌面壁纸和常见二次元图片均适用。

## 重要的准确性边界

通用视觉模型可判断「二次元插画」并生成服装、发型、构图、情绪等标签；但**不应把它输出的具体角色名当成事实**。冷门角色、同人图、遮挡图最容易误判，所以接口会给角色候选附上置信度，并要求低把握时返回空数组。

如果你的图片库以动漫图为主，下一步可加一个本地 Danbooru/JoyTag 分类器作为第二个引擎：它擅长细粒度动漫属性 tag，但同样无法保证冷门角色识别。先用当前服务收集一批真实 QQ 图片并抽样评估，再决定是否需要这一步或为常见角色建立小型参考图库，会更可靠。

## 安装与启动

在 Mac 的本项目目录执行：

```zsh
chmod +x setup.sh start-lan.sh
./setup.sh
export TAGGER_API_KEY='换成至少 24 位的随机字符串'
./start-lan.sh
```

首次启动会下载模型，完成加载后服务监听 `0.0.0.0:8787`。请在 macOS 防火墙中只允许你的私有局域网访问端口 8787；不要在路由器上做端口映射，也不要把它暴露到互联网。

项目的虚拟环境、模型权重和 Hugging Face 缓存都会保存在当前目录下的 `.venv/` 与 `models/`；不会再使用系统的模型缓存目录。

获取 Mac 局域网 IP：

```zsh
ipconfig getifaddr en0
```

## 浏览器测试页面

服务启动后，在 Mac 或 Windows 浏览器打开：

```text
http://MAC的局域网IP:8787/
```

输入与 Mac 终端中相同的 `TAGGER_API_KEY`，然后拖入图片或点击选择。页面会显示预览、分类、tags、角色候选和摘要；密钥仅保存在当前浏览器标签页中。

页面也能在 4B（更快）和 8B（更细致）间切换。切换会暂停所有识别请求并重新加载模型；首次使用 8B 会下载约 5.8GB 到 `models/`。也可以在启动时指定默认模型：

```zsh
MODEL_PRESET=8b ./start-lan.sh
```

## Windows 调用

PowerShell 示例：

```powershell
$headers = @{ "X-API-Key" = "与 Mac 上相同的密钥" }
Invoke-RestMethod -Method Post `
  -Uri "http://MAC的局域网IP:8787/v1/tag" `
  -Headers $headers `
  -Form @{ image = Get-Item "C:\\Images\\qq-picture.jpg" }
```

健康检查（无需密钥）：

```powershell
Invoke-RestMethod "http://MAC的局域网IP:8787/healthz"
```

成功结果示例：

```json
{
  "category": "anime_illustration",
  "tags": ["二次元插画", "蓝色长发", "校服", "室内", "半身像", "柔和光线"],
  "character_candidates": [{"name": "...", "confidence": 0.82}],
  "contains_text": false,
  "summary": "一张室内光线下的二次元人物插画。",
  "model": "mlx-community/Qwen3-VL-4B-Instruct-4bit"
}
```

`category` 取值固定为：`anime_illustration`、`chat_screenshot`、`meme_or_sticker`、`casual_photo`、`portrait_photo`、`professional_photography`、`wallpaper_or_desktop`、`document_or_ui`、`other`。

## 性能调节

8B 更擅长细节理解，但在这台 16 GB Mac 上单张图约 30 秒是可能的，尤其是 2K 截图。日常分类建议使用 4B；若保留 8B 又希望更快，可降低输入图片最长边：

```zsh
MODEL_PRESET=8b MAX_IMAGE_EDGE=1280 ./start-lan.sh
```

角色候选会附带置信度；页面即使没有候选也会明确显示“未识别”，而不再隐藏该区域。
