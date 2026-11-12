# Remote Mac 图片 Tag Web UI

Web 控制台运行在 8788 端口，并通过手动输入的服务端目录批量调用 Mac 上的图片 Tag 服务。

目录路径是运行本 Web 服务机器上的路径，而不是浏览器所在电脑的路径。例如 Web 服务运行在本机 Mac 时，可输入 /Volumes/分类/...；也可输入 smb://服务器/共享/子目录，控制台会转换为已挂载的 /Volumes/共享/子目录。若共享尚未挂载，需先在运行 Web 服务的 Mac 上通过访达连接它。

## 启动

    cd /Users/yangxing/remote_web
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

    # 对外发布时，设置至少 24 位的随机访问口令
    export REMOTE_WEB_ACCESS_TOKEN='replace-with-a-long-random-secret'

    # 可选：只允许调用指定的 Mac / VPN 主机
    export TAGGER_ALLOWED_HOSTS='192.168.1.23,100.64.0.10'

    # 可选：只允许扫描这些路径范围
    export IMAGE_SOURCE_ROOTS='/Volumes/分类,/data/photos'

    chmod +x start.sh
    ./start.sh

在浏览器打开 http://服务器地址:8788/。8787 的原单图测试页已经包含自动跳转到本控制台的链接。

功能包括：手动目录、路径可读性检查、图片数量统计、检查已有成功 Tag、手动指定结果保存路径、递归扫描、跳过已有成功项、数量/时长限制、暂停/停止、模型读取/切换，以及 CSV、JSONL 和任务摘要下载。结果保存路径留空时使用 reports/；填写其他路径时会在该位置创建带时间戳的任务子目录，不会包含 API key。

模型选项由 Mac 服务 `/v1/models` 返回的 `presets` 动态生成，按完整模型 ID 匹配当前模型。填写服务地址和 API key 后自动读取，也可点击“读取模型”刷新；地址、密钥或访问口令更改后旧列表失效。读取失败时禁用切换。可选预设只由 Mac 服务校验，8788 不再限制为 4B/8B；添加模型只需更新 Mac 服务的 `MODEL_PRESETS` 并重启 8787，再读取列表。

请保持 Mac 上 8787 服务只在私有网络中可访问；对外发布此控制台时建议设置访问口令、目标主机白名单，并由反向代理提供 HTTPS。
