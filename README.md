# Local model on Mac mini M4, with Windows clients

一个运行在 Apple Silicon Mac 上、供局域网 Windows 电脑调用的本地图片 Tag 服务。

## 目录

- [`mac/`](mac/)：Mac 端本地推理服务、内置测试网页与安装/启动脚本。
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
