# Local model on Mac mini M4, with Windows clients

一个面向**可信局域网**的个人图片整理工作流：Apple Silicon Mac 在本机运行视觉模型，Windows 电脑将图片发送到 Mac 获取中文 Tag，再按 Tag 安全归档到本地图片库。项目不包含模型权重，不调用云端 AI 服务。

> 这是 HTTP 局域网服务，不是公网服务。图片与 API key 会在局域网内传输；请仅在可信网络使用，勿将 8787 端口映射到公网。

## 工作流

```text
Windows 图片预处理（可选）
        ↓
Windows 客户端 → Mac 本地 Tag 服务 → CSV / JSONL 报告
        ↓
Windows 安全归档 → 分类图片库 + SQLite / CSV 标签库
```

也可以使用 Windows AIO 控制中心统一启动三个工具，并在已识别图片达到指定数量或等待时间后，自动调用安全归档流程。

## 仓库内容

| 路径 | 作用 |
|---|---|
| [`mac/`](mac/) | Mac 端 FastAPI 服务、内置测试页及安装/启动脚本。 |
| [`win/preprocessing/`](win/preprocessing/) | QQ 群聊图片缓存预分类：预览、复制或移动。 |
| [`win/tagging_by_mac/`](win/tagging_by_mac/) | Windows 图形客户端：向 Mac 提交图片并实时写入 Tag 报告。 |
| [`win/moving/`](win/moving/) | Windows 安全归档：复制、SHA-256 校验、登记数据库后才删除原图。 |
| [`win/AIO/`](win/AIO/) | 三项工作流的启动与自动归档控制中心。 |
| [`WINDOWS_CLIENT_INTEGRATION.md`](WINDOWS_CLIENT_INTEGRATION.md) | 调用 Tag HTTP API 的完整协议、示例和故障排查。 |

## Mac：启动本地 Tag 服务

要求：Apple Silicon Mac、Homebrew Python 3.13（或通过 `PYTHON_BIN` 指向兼容的 Python 3.11+）、约 16 GB 统一内存；首次运行需要下载模型。

```zsh
git clone https://github.com/lolikongpoi/localmodel-base_on_macminim4-win.git
cd localmodel-base_on_macminim4-win/mac
chmod +x setup.sh start-lan.sh
./setup.sh
export TAGGER_API_KEY='替换为至少 24 位的随机密钥'
./start-lan.sh
```

默认监听 `0.0.0.0:8787`。在 Mac 上运行 `ipconfig getifaddr en0` 可查看常见 Wi-Fi 网卡的局域网地址；然后在同一网络的浏览器打开 `http://MAC_LAN_IP:8787/` 做单图测试。

默认模型为 Qwen3-VL 4B 量化版；8B 预设更慢且首次会下载更多文件。模型、性能、HEIC 和安全边界见 [`mac/README.md`](mac/README.md)。

## Windows：首次准备

要求：Windows 10/11、Python 3.10+。各工具的环境彼此独立；只安装你实际需要的部分。

1. 预处理：双击 `win\preprocessing\安装环境.bat`，再双击 `启动预分类.bat`。
2. Mac Tag 客户端：双击 `win\tagging_by_mac\安装环境.bat`，再双击 `启动Mac图片Tag客户端.bat`。
3. 手动安全归档：在仓库根目录执行：

   ```powershell
   cd win
   py -3 -m venv .venv
   ```

   然后双击 `win\moving\启动图片Tag安全归档.bat`。
4. AIO：先完成第 2 步，再双击 `win\AIO\启动AIO控制中心.bat`。若要在 AIO 中打开预处理，也需要先完成第 1 步。

Windows 客户端与 Mac 服务的连接、API key、超时和错误处理详见 [`WINDOWS_CLIENT_INTEGRATION.md`](WINDOWS_CLIENT_INTEGRATION.md)。

## 数据与安全

- Tag 客户端的 API key 仅保存在运行内存；示例配置不会包含 key。
- 预处理默认是 `preview`，不会改动原图片；`move` 和“删除 ≤10KB 文件”会明确二次确认。
- 安全归档先将全部候选文件复制到临时目录并逐个做 SHA-256 校验。只有全部校验和首次数据库快照同步成功后，才删除原图。
- AIO 的自动归档默认关闭，并在开启时要求确认；它沿用相同的安全归档逻辑。
- `G:\image` 和 F 盘缓存路径只是示例。请在界面中选择自己的目标图片库，并在首次大规模移动前用少量可复制的测试图片验证流程。

报告、数据库字段和归档恢复说明见 [`win/moving/README.md`](win/moving/README.md)。

## AI 开发与使用说明

本项目的全部程序代码由 **GPT-5.6 Terra** 生成，没有人工编写的业务代码。AI 生成不等于已经过独立安全审计或适合所有环境；使用者应自行判断并承担在自己设备上运行的风险。

建议在安装、运行或修改前：

- 先使用 AI Review 或其他代码审查方式阅读将要运行的脚本、依赖和文件操作逻辑；尤其关注自动归档、移动与删除流程。
- 在独立的虚拟环境中安装依赖，不要把 API key、图片、报告或标签数据库提交到 Git。
- 先用可丢弃的少量测试图片完整走一遍预处理、Tag 与归档流程；确认结果和备份策略后再处理正式图片库。
- 将服务限制在可信局域网，并在升级依赖、模型或 AI 生成代码后重新测试。

## 发布与许可

本仓库代码采用 [MIT License](LICENSE)。模型权重与第三方依赖不随仓库分发，使用前请自行阅读其许可条款；Qwen3-VL 及 MLX-VLM 的版本和兼容性以各自上游发布为准。

发布前请至少完成：Mac 单图测试、Windows 客户端连通性测试、一次预处理预览，以及使用可丢弃样本进行一次安全归档验证。
