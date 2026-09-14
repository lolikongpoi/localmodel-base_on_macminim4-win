# Windows 图片 Tag 安全归档

这是 Windows 端的本地归档工具。它读取图片 Tag 报告，将已经成功识别的图片按类别归档到目标图片库，并保存可检索的 SQLite 标签库和 CSV 清单。

本工具不调用云端服务，也不向 Mac 上传图片。它只读取已有的 `image_tags.csv` 报告；报告通常由连接 Mac 局域网 Tag 服务的 Windows 客户端生成。

## 功能

- 读取一个或多个 Tag 运行目录中的 `image_tags.csv`，对同一原图采用最新一条 `success` 记录。
- 按服务返回的 `category` 创建中文分类目录，例如 `二次元插画`、`聊天截图`、`生活照片`、`摄影作品`。
- 将随机原始文件名改为可读名称：最多三个内容 Tag 加稳定短编号，例如 `黑发_校服_楼梯__a1b2c3d4e5.png`。
- 先完整复制到临时目录并逐张进行 SHA-256 校验；所有副本验证通过后才开始删除原图。
- 将标签、摘要、模型、角色候选、源路径、目标路径和移动状态保存到 SQLite；同时导出 UTF-8 BOM CSV，可直接用 Excel 打开。
- 界面持续显示完整进度：复制和校验、整理分类目录、删除已验证原图三个阶段。

## 安全流程

点击“扫描并生成计划”不会移动文件。确认“安全移动：复制后删除原图”后，程序严格执行：

1. 复制全部计划图片到目标库的 `.tag_move_staging` 临时目录。
2. 对每个副本验证文件大小和 SHA-256。
3. 整理已验证副本到最终分类目录。
4. 先在 F 盘缓存写入数据库，并将一份“已复制并校验”快照同步到目标图片库。
5. 逐张删除原图，更新 F 盘缓存数据库并导出 CSV。
6. 将数据库和 CSV 的完整快照一次性同步到目标图片库。

复制、校验、数据库写入或第一次数据库同步失败时，不会删除原图。若最后删除个别原图失败，副本保留，数据库状态会写为 `copied_verified`。

## 安装与启动

要求：Windows 10/11，Python 3.10 或更高版本。程序仅使用 Python 标准库和 Tkinter。

在仓库根目录执行：

```powershell
cd win
py -3 -m venv .venv
.\.venv\Scripts\python.exe .\moving\tag_move_app.py
```

也可以在创建环境后双击 `win\moving\启动图片Tag安全归档.bat`。

## 使用方法

1. 启动程序，填写或选择 Tag 报告目录。默认位置为 `win\mac_tag_reports`；若报告位于其他位置，请直接在界面中选择。
2. 填写目标图片库，例如 `G:\image`。
3. 点击“扫描并生成计划”，检查可移动、原图缺失或目标冲突的数量。
4. 点击“安全移动：复制后删除原图”，确认后开始执行。
5. 需要将已有数据库转换为 CSV 或重新同步时，点击“更新数据库与 CSV”。

## 数据位置与 HDD 优化

以 `G:\image` 为目标库时：

- F 盘缓存数据库与 CSV：`win\moving\cache\<目标库编号>\image_tags.db`、`image_tags.csv`。
- G 盘数据库快照：`G:\image\image_tags.db`。
- G 盘可读清单：`G:\image\image_tags.csv`。

数据库的逐条更新和 CSV 生成均在 F 盘缓存进行；G 盘只在一个批次结束时接收完整快照，避免机械硬盘的频繁随机写入。

## 数据库与 CSV 字段

SQLite 的 `images` 表记录每张图片的源路径、目标路径、分类、Tags、摘要、模型、文字标记、角色候选、文件大小和移动状态。`image_tags` 表提供按单个 Tag 查询的索引。

CSV 记录以下字段：

```text
source_path,destination_path,original_filename,category,tags,summary,
contains_text,auto_character,character_candidates,model,report_path,
file_size,copied_at,moved_at,move_status,error
```

## 注意事项

- `G:\image` 是本地示例路径，可在界面中改为其他目标目录；每个目标目录拥有独立的 F 盘缓存。
- 不要手动删除 F 盘缓存中的数据库；它是归档程序的主数据库。G 盘版本是面向其他程序读取的同步快照。
- 如果以其他程序修改了 G 盘数据库，请先备份；归档程序会将 F 盘缓存重新同步到 G 盘。
