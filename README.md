# Local model on Mac mini M4, with Windows clients

一个运行在 Apple Silicon Mac 上、供局域网 Windows 电脑调用的本地图片 Tag 服务。

## 目录

- [`mac/`](mac/)：Mac 端本地推理服务、内置测试网页与安装/启动脚本。
- [`win/tagging_by_mac/`](win/tagging_by_mac/)：独立的 Windows 图形客户端；通过局域网把图片提交给 Mac 端服务，并实时保存 CSV 与 JSONL Tag 报告。
- [`win/moving/`](win/moving/)：Windows 图片 Tag 安全归档工具；按 Tag 分类、可读重命名、先复制校验再删除原图，并生成 SQLite 与 CSV 标签库。
- [`WINDOWS_CLIENT_INTEGRATION.md`](WINDOWS_CLIENT_INTEGRATION.md)：Windows 客户端接入 API 的完整说明。

## Mac 端快速开始

```zsh
git clone https://github.com/lolikongpoi/localmodel-base_on_macminim4-win.git
cd localmodel-base_on_macminim4-win/mac
chmod +x setup.sh start-lan.sh
./setup.sh
export TAGGER_API_KEY='请使用至少 24 位的随机密钥'
./start-lan.sh
```

启动后在 Mac 或同一局域网内的 Windows 浏览器中打开：

```text
http://MAC_LAN_IP:8787/
```

详细的模型、性能、HEIC 支持与安全说明见 [`mac/README.md`](mac/README.md)。

## Windows 图片归档

若已通过 Windows 客户端生成图片 Tag 报告，可使用 [`win/moving/`](win/moving/) 将图片安全归档到本地图片库。工具会先完整复制和 SHA-256 校验，随后才删除原图；标签数据库先写入 F 盘缓存，最后以完整快照同步到目标图片库和可读 CSV。详见 [`win/moving/README.md`](win/moving/README.md)。

## Windows 端图片 Tag 客户端

[`win/tagging_by_mac/`](win/tagging_by_mac/) 提供 Windows 图形界面：填写 Mac 的局域网服务地址与 API key 后，可递归扫描图片、按顺序上传识别，并将每张图片的分类、Tags、角色候选和摘要实时写入 CSV、JSONL。它使用目录内独立的 Python 环境，运行时不依赖上一级目录；首次使用请先运行 `安装环境.bat`，再运行 `启动Mac图片Tag客户端.bat`。详见该目录的 [README](win/tagging_by_mac/README.md)。
