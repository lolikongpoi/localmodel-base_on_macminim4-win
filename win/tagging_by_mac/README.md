# 通过 Mac 给图片加 Tag（Windows 客户端）

这个目录是 Windows 端客户端：它将本地图片通过局域网逐张发送到 Mac 上运行的 Tag 服务，并把结果保存为 CSV 和 JSONL。不会调用云端服务；API key 仅保存在运行内存中。

## 开始使用

1. 在 Mac 上启动仓库 `mac/` 目录里的局域网 Tag 服务，并记录其局域网地址和 API key。
2. 双击 `安装环境.bat` 创建本目录独立的 Windows 端 Python 环境（需要 Python 3.10 或更高版本）。
3. 双击 `启动Mac图片Tag客户端.bat`，输入 Mac 地址（例如 `http://192.168.1.23:8787`）和 API key。
4. 选择图片目录，先点击“检查联通”，然后开始处理。

每次运行会在选择的报告位置创建一个时间戳目录，其中的 `image_tags.csv` 和 `image_tags.jsonl` 会逐张实时写入；可勾选跳过已经成功处理的图片，方便中断后继续。

## 文件说明

- `mac_tag_app.py`：Windows 图形界面。
- `mac_tag_client.py`：图片扫描、HTTP 通信和报告写入逻辑。
- `mock_mac_tag_service.py`：仅供本机验证客户端流程的模拟服务。
- `mac_tag_task_config.example.json`：脱敏配置模板；复制为 `mac_tag_task_config.json` 后可由界面读取和保存。

## 本机验证（不连接 Mac）

先双击 `启动本地模拟Tag服务.bat`，再启动客户端并填写：

- 地址：`http://127.0.0.1:8787`
- API key：`test-local-key`

模拟服务只返回固定示例标签，不会运行 AI 模型。

## 运行环境路径

启动脚本只使用本目录的 `.venv`，不会在运行时依赖上一级 `本地识别` 目录。首次运行若尚无 `.venv`，`安装环境.bat` 会使用上一级已有环境或系统 Python 来创建本目录的独立环境。
