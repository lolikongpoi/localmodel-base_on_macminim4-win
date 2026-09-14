from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT_ROOT = PROJECT_DIR.parent / "mac_tag_reports"
DEFAULT_LIBRARY_ROOT = Path(r"G:\image")
DATABASE_NAME = "image_tags.db"
CSV_NAME = "image_tags.csv"
STAGING_DIR_NAME = ".tag_move_staging"
CACHE_DIR_NAME = "cache"

CATEGORY_FOLDERS = {
    "anime_illustration": "二次元插画",
    "chat_screenshot": "聊天截图",
    "meme_or_sticker": "表情包与贴图",
    "casual_photo": "生活照片",
    "portrait_photo": "人像照片",
    "professional_photography": "摄影作品",
    "wallpaper_or_desktop": "壁纸与桌面",
    "document_or_ui": "文档与界面",
    "other": "其他",
}

GENERIC_TAGS = {
    "动漫插画", "二次元插画", "插画", "动漫风格", "日系动漫风格", "女性角色", "男性角色",
    "角色", "图片", "照片", "anime illustration", "anime style", "illustration",
}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class TagEntry:
    source: Path
    source_key: str
    category: str
    tags: tuple[str, ...]
    summary: str
    contains_text: bool | None
    auto_character: str
    character_candidates_json: str
    model: str
    report_path: Path


@dataclass(frozen=True)
class MovePlan:
    entry: TagEntry
    destination: Path | None
    state: str
    note: str


class TransferError(RuntimeError):
    def __init__(self, message: str, staging_dir: Path | None = None):
        super().__init__(message)
        self.staging_dir = staging_dir


def normalize_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def parse_tags(raw: str) -> tuple[str, ...]:
    seen: set[str] = set()
    tags: list[str] = []
    for tag in raw.split("|"):
        tag = WHITESPACE.sub(" ", tag.strip())
        key = tag.casefold()
        if tag and key not in seen:
            tags.append(tag)
            seen.add(key)
    return tuple(tags)


def parse_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def load_latest_successful_tags(report_root: Path) -> list[TagEntry]:
    """读取每个原图最新的成功 Tag；未完成报告中的成功行同样有效。"""
    if not report_root.is_dir():
        return []
    chosen: dict[str, tuple[tuple[float, int], TagEntry]] = {}
    reports = sorted(
        report_root.glob("*/image_tags.csv"),
        key=lambda path: (path.stat().st_mtime, path.parent.name),
    )
    for report_path in reports:
        try:
            modified = report_path.stat().st_mtime
            with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row_number, row in enumerate(csv.DictReader(handle), start=1):
                    if (row.get("status") or "").strip().lower() != "success":
                        continue
                    source_text = (row.get("source") or "").strip()
                    if not source_text:
                        continue
                    source = Path(source_text)
                    source_key = normalize_path(source)
                    entry = TagEntry(
                        source=source,
                        source_key=source_key,
                        category=(row.get("category") or "other").strip() or "other",
                        tags=parse_tags(row.get("tags") or ""),
                        summary=(row.get("summary") or "").strip(),
                        contains_text=parse_bool(row.get("contains_text") or ""),
                        auto_character=(row.get("auto_character") or "").strip(),
                        character_candidates_json=(row.get("character_candidates") or "[]").strip() or "[]",
                        model=(row.get("model") or "").strip(),
                        report_path=report_path,
                    )
                    order = (modified, row_number)
                    previous = chosen.get(source_key)
                    if previous is None or order >= previous[0]:
                        chosen[source_key] = (order, entry)
        except (OSError, UnicodeError, csv.Error):
            # 读取某一份损坏或正在写入的报告失败时，其他报告仍可正常使用。
            continue
    return sorted((item[1] for item in chosen.values()), key=lambda item: item.source_key)


def category_folder(category: str) -> str:
    return CATEGORY_FOLDERS.get(category, "其他")


def safe_component(value: str, limit: int = 18) -> str:
    value = INVALID_FILENAME.sub("", value)
    value = WHITESPACE.sub("_", value).strip(" ._")
    if not value:
        return ""
    return value[:limit].rstrip(" ._")


def readable_filename(entry: TagEntry) -> str:
    parts: list[str] = []
    for tag in entry.tags:
        cleaned = safe_component(tag)
        if not cleaned or cleaned.casefold() in GENERIC_TAGS:
            continue
        if cleaned.casefold() not in {part.casefold() for part in parts}:
            parts.append(cleaned)
        if len(parts) == 3:
            break
    if not parts:
        summary_part = safe_component(entry.summary, 32)
        parts.append(summary_part or "未命名图片")
    short_id = hashlib.sha1(entry.source_key.encode("utf-8")).hexdigest()[:10]
    extension = entry.source.suffix.lower() or ".img"
    return "_".join(parts) + f"__{short_id}{extension}"


def database_path(library_root: Path) -> Path:
    return library_root / DATABASE_NAME


def csv_export_path(library_root: Path) -> Path:
    return library_root / CSV_NAME


def cache_root(library_root: Path) -> Path:
    """每个目标图片库使用独立的 F 盘缓存，避免切换目标目录时混用标签库。"""
    library_id = hashlib.sha1(normalize_path(library_root).encode("utf-8")).hexdigest()[:12]
    return PROJECT_DIR / CACHE_DIR_NAME / library_id


def cached_database_path(library_root: Path) -> Path:
    return cache_root(library_root) / DATABASE_NAME


def cached_csv_path(library_root: Path) -> Path:
    return cache_root(library_root) / CSV_NAME


def copy_snapshot(source: Path, destination: Path) -> Path:
    """将缓存文件作为完整快照一次性镜像到目标目录，不在 HDD 上做逐条写入。"""
    if not source.is_file():
        raise FileNotFoundError(f"找不到需要同步的缓存文件：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError(f"数据库快照大小不一致：{destination.name}")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return destination


def prepare_cached_database(library_root: Path) -> Path:
    """首次使用时从 G 盘导入旧数据库；之后 F 盘缓存是唯一写入位置。"""
    cached = cached_database_path(library_root)
    if cached.is_file():
        return cached
    existing = database_path(library_root)
    cached.parent.mkdir(parents=True, exist_ok=True)
    if existing.is_file():
        copy_snapshot(existing, cached)
    return cached


def read_database_rows(db_path: Path) -> dict[str, tuple[str, str]]:
    if not db_path.is_file():
        return {}
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT source_path, destination_path, move_status FROM images"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {source: (destination, status) for source, destination, status in rows}


def database_for_reading(library_root: Path) -> Path:
    """优先使用 F 盘缓存；首次迁移前可只读现有 G 盘数据库。"""
    cached = cached_database_path(library_root)
    return cached if cached.is_file() else database_path(library_root)


def make_plans(entries: list[TagEntry], library_root: Path) -> list[MovePlan]:
    existing = read_database_rows(database_for_reading(library_root))
    plans: list[MovePlan] = []
    for entry in entries:
        source = entry.source
        destination = library_root / category_folder(entry.category) / readable_filename(entry)
        if normalize_path(source).startswith(normalize_path(library_root) + os.sep):
            plans.append(MovePlan(entry, destination, "already_in_library", "原图已位于目标图片库中"))
        elif not source.is_file():
            old = existing.get(entry.source_key)
            if old and Path(old[0]).is_file() and old[1] == "moved":
                plans.append(MovePlan(entry, Path(old[0]), "already_moved", "已在数据库中登记为已移动"))
            else:
                plans.append(MovePlan(entry, destination, "source_missing", "找不到原图，未移动"))
        elif destination.exists():
            old = existing.get(entry.source_key)
            if old and normalize_path(old[0]) == normalize_path(destination):
                plans.append(MovePlan(entry, destination, "recovery", "发现此前已复制的目标文件，可继续完成移动"))
            else:
                plans.append(MovePlan(entry, destination, "destination_conflict", "目标文件已存在，为避免覆盖而跳过"))
        else:
            plans.append(MovePlan(entry, destination, "ready", "准备复制、校验并移动"))
    return plans


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_copy(source: Path, copied: Path) -> None:
    if not copied.is_file() or copied.stat().st_size != source.stat().st_size:
        raise TransferError(f"复制校验失败：{source.name} 的文件大小不一致")
    if file_sha256(source) != file_sha256(copied):
        raise TransferError(f"复制校验失败：{source.name} 的 SHA-256 不一致")


def open_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
            source_path TEXT PRIMARY KEY,
            destination_path TEXT NOT NULL UNIQUE,
            original_filename TEXT NOT NULL,
            category TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            contains_text INTEGER,
            auto_character TEXT NOT NULL,
            character_candidates_json TEXT NOT NULL,
            model TEXT NOT NULL,
            report_path TEXT NOT NULL,
            file_size INTEGER,
            copied_at TEXT,
            moved_at TEXT,
            move_status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS image_tags (
            source_path TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (source_path, tag),
            FOREIGN KEY (source_path) REFERENCES images(source_path) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_images_category ON images(category);
        CREATE INDEX IF NOT EXISTS idx_images_status ON images(move_status);
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON image_tags(tag);
        """
    )
    return connection


def record_verified_copies(connection: sqlite3.Connection, plans: list[MovePlan]) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with connection:
        for plan in plans:
            assert plan.destination is not None
            entry = plan.entry
            connection.execute(
                """
                INSERT INTO images (
                    source_path, destination_path, original_filename, category, tags_json, summary,
                    contains_text, auto_character, character_candidates_json, model, report_path,
                    file_size, copied_at, moved_at, move_status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'copied_verified', '')
                ON CONFLICT(source_path) DO UPDATE SET
                    destination_path=excluded.destination_path,
                    original_filename=excluded.original_filename,
                    category=excluded.category,
                    tags_json=excluded.tags_json,
                    summary=excluded.summary,
                    contains_text=excluded.contains_text,
                    auto_character=excluded.auto_character,
                    character_candidates_json=excluded.character_candidates_json,
                    model=excluded.model,
                    report_path=excluded.report_path,
                    file_size=excluded.file_size,
                    copied_at=excluded.copied_at,
                    move_status='copied_verified',
                    error=''
                """,
                (
                    entry.source_key,
                    str(plan.destination),
                    entry.source.name,
                    entry.category,
                    json.dumps(entry.tags, ensure_ascii=False),
                    entry.summary,
                    None if entry.contains_text is None else int(entry.contains_text),
                    entry.auto_character,
                    entry.character_candidates_json,
                    entry.model,
                    str(entry.report_path),
                    entry.source.stat().st_size,
                    now,
                ),
            )
            connection.execute("DELETE FROM image_tags WHERE source_path = ?", (entry.source_key,))
            connection.executemany(
                "INSERT OR IGNORE INTO image_tags (source_path, tag) VALUES (?, ?)",
                [(entry.source_key, tag) for tag in entry.tags],
            )


def set_move_status(connection: sqlite3.Connection, plan: MovePlan, status: str, error: str = "") -> None:
    with connection:
        connection.execute(
            "UPDATE images SET move_status = ?, moved_at = ?, error = ? WHERE source_path = ?",
            (status, datetime.now().astimezone().isoformat(timespec="seconds"), error, plan.entry.source_key),
        )


def export_readable_csv(connection: sqlite3.Connection, csv_path: Path) -> Path:
    """从 SQLite 导出 UTF-8 BOM CSV，便于 Excel 和普通脚本直接读取。"""
    rows = connection.execute(
        """
        SELECT source_path, destination_path, original_filename, category, tags_json, summary,
               contains_text, auto_character, character_candidates_json, model, report_path,
               file_size, copied_at, moved_at, move_status, error
        FROM images
        ORDER BY category, destination_path
        """
    ).fetchall()
    fields = [
        "source_path", "destination_path", "original_filename", "category", "tags", "summary",
        "contains_text", "auto_character", "character_candidates", "model", "report_path",
        "file_size", "copied_at", "moved_at", "move_status", "error",
    ]
    temporary = csv_path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            try:
                tags = " | ".join(json.loads(row[4]))
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = row[4]
            writer.writerow({
                "source_path": row[0],
                "destination_path": row[1],
                "original_filename": row[2],
                "category": row[3],
                "tags": tags,
                "summary": row[5],
                "contains_text": "" if row[6] is None else bool(row[6]),
                "auto_character": row[7],
                "character_candidates": row[8],
                "model": row[9],
                "report_path": row[10],
                "file_size": row[11],
                "copied_at": row[12],
                "moved_at": row[13],
                "move_status": row[14],
                "error": row[15],
            })
    temporary.replace(csv_path)
    return csv_path


def synchronize_cached_database(library_root: Path) -> dict[str, Path]:
    """将 F 盘缓存库导出为 CSV，并将 DB/CSV 的完整快照同步到目标图片库。"""
    cached_db = prepare_cached_database(library_root)
    connection = open_database(cached_db)
    try:
        cached_csv = export_readable_csv(connection, cached_csv_path(library_root))
    finally:
        connection.close()
    target_db = copy_snapshot(cached_db, database_path(library_root))
    target_csv = copy_snapshot(cached_csv, csv_export_path(library_root))
    return {
        "cached_db": cached_db,
        "cached_csv": cached_csv,
        "target_db": target_db,
        "target_csv": target_csv,
    }


def execute_safe_move(
    plans: list[MovePlan], library_root: Path, emit: callable,
) -> dict[str, object]:
    """先完整复制+SHA 校验所有文件，随后登记数据库，最后才删除原图。"""
    workable = [plan for plan in plans if plan.state in {"ready", "recovery"}]
    if not workable:
        return {"moved": 0, "delete_failed": [], "staging": None, "csv_path": None, "csv_error": ""}

    library_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_root = library_root / STAGING_DIR_NAME / run_id
    staged: dict[MovePlan, Path] = {}
    try:
        for index, plan in enumerate(workable, start=1):
            assert plan.destination is not None
            emit("copy", (index, len(workable), plan))
            stage_path = staging_root / plan.destination.relative_to(library_root)
            if plan.state == "recovery":
                verify_copy(plan.entry.source, plan.destination)
                continue
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plan.entry.source, stage_path)
            verify_copy(plan.entry.source, stage_path)
            staged[plan] = stage_path

        for index, plan in enumerate(workable, start=1):
            assert plan.destination is not None
            emit("promote", (index, len(workable), plan))
            if plan.state == "recovery":
                continue
            stage_path = staged[plan]
            plan.destination.parent.mkdir(parents=True, exist_ok=True)
            if plan.destination.exists():
                raise TransferError(f"目标文件在执行期间出现，未覆盖：{plan.destination}", staging_root)
            stage_path.replace(plan.destination)

        cached_db = prepare_cached_database(library_root)
        connection = open_database(cached_db)
        try:
            record_verified_copies(connection, workable)
        finally:
            connection.close()

        # 删除原图前，先确保目标库已有“已复制并校验”状态的数据库快照。
        copy_snapshot(cached_db, database_path(library_root))

        connection = open_database(cached_db)
        try:
            delete_failed: list[tuple[MovePlan, str]] = []
            moved = 0
            for index, plan in enumerate(workable, start=1):
                emit("delete", (index, len(workable), plan))
                try:
                    plan.entry.source.unlink()
                except OSError as exc:
                    message = str(exc)
                    set_move_status(connection, plan, "copied_verified", message)
                    delete_failed.append((plan, message))
                else:
                    set_move_status(connection, plan, "moved")
                    moved += 1
            cached_csv = export_readable_csv(connection, cached_csv_path(library_root))
        finally:
            connection.close()

        try:
            copy_snapshot(cached_db, database_path(library_root))
            csv_path = copy_snapshot(cached_csv, csv_export_path(library_root))
            csv_error = ""
        except OSError as exc:
            csv_path = None
            csv_error = (
                f"G 盘数据库或 CSV 快照同步失败：{exc}。"
                f"F 盘缓存仍完整保留在 {cache_root(library_root)}"
            )
    except TransferError as exc:
        if exc.staging_dir is None:
            raise TransferError(str(exc), staging_root) from exc
        raise
    except Exception as exc:
        raise TransferError(str(exc), staging_root) from exc
    finally:
        if staging_root.is_dir() and not any(staging_root.rglob("*")):
            staging_root.rmdir()

    return {
        "moved": moved,
        "delete_failed": delete_failed,
        "staging": staging_root,
        "csv_path": csv_path,
        "csv_error": csv_error,
    }


class TagMoveApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("图片 Tag 安全归档")
        self.geometry("1300x780")
        self.minsize(980, 620)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.plans: list[MovePlan] = []
        self.busy = False
        self._build_ui()
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.report_var = tk.StringVar(value=str(DEFAULT_REPORT_ROOT))
        self.library_var = tk.StringVar(value=str(DEFAULT_LIBRARY_ROOT))
        self.status_var = tk.StringVar(value="选择 Tag 报告目录后，点击“扫描并生成计划”。")
        self.phase_var = tk.StringVar(value="移动进度：等待开始")
        self.result_var = tk.StringVar(value="结果：尚未执行移动")
        self.summary_var = tk.StringVar(value="尚未扫描")

        self._path_row(form, 0, "Tag 报告目录", self.report_var, "选择…", self._choose_reports)
        self._path_row(form, 1, "目标图片库", self.library_var, "选择…", self._choose_library)
        note = ttk.Label(
            form,
            text="数据库先写入 F 盘缓存，再批量镜像到目标图片库。流程：完整复制 → SHA-256 校验 → 写入缓存库 → 删除原图。",
        )
        note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        actions = ttk.Frame(form)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.scan_button = ttk.Button(actions, text="扫描并生成计划", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.move_button = ttk.Button(actions, text="安全移动：复制后删除原图", command=self._start_move, state="disabled")
        self.move_button.pack(side="left", padx=(8, 0))
        self.sync_button = ttk.Button(actions, text="更新数据库与 CSV", command=self._start_sync)
        self.sync_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="打开目标文件夹", command=self._open_library).pack(side="left", padx=(16, 0))
        ttk.Label(actions, textvariable=self.summary_var).pack(side="right")

        info = ttk.Frame(self, padding=(12, 0, 12, 3))
        info.pack(fill="x")
        ttk.Label(info, textvariable=self.status_var).pack(anchor="w")

        progress_info = ttk.Frame(self, padding=(12, 0, 12, 3))
        progress_info.pack(fill="x")
        ttk.Label(progress_info, textvariable=self.phase_var).pack(side="left")
        self.progress = ttk.Progressbar(progress_info, mode="determinate", length=340)
        self.progress.pack(side="right")

        result_info = ttk.Frame(self, padding=(12, 0, 12, 6))
        result_info.pack(fill="x")
        ttk.Label(result_info, textvariable=self.result_var).pack(anchor="w")

        table_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        table_frame.pack(fill="both", expand=True)
        columns = ("state", "source", "category", "tags", "destination", "note")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        labels = {
            "state": "状态", "source": "原图", "category": "分类", "tags": "主要 Tag",
            "destination": "目标文件", "note": "说明",
        }
        widths = {"state": 120, "source": 240, "category": 120, "tags": 230, "destination": 310, "note": 240}
        for column in columns:
            self.table.heading(column, text=labels[column])
            self.table.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, button: str, command: callable) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text=button, command=command).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)

    def _choose_reports(self) -> None:
        value = filedialog.askdirectory(title="选择 mac_tag_reports 目录")
        if value:
            self.report_var.set(value)

    def _choose_library(self) -> None:
        value = filedialog.askdirectory(title="选择目标图片库；不存在时可直接填写 G:\\image")
        if value:
            self.library_var.set(value)

    def _open_library(self) -> None:
        target = Path(self.library_var.get().strip())
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
        except OSError as exc:
            messagebox.showerror("无法打开目标文件夹", str(exc))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.move_button.configure(state="disabled" if busy or not any(plan.state in {"ready", "recovery"} for plan in self.plans) else "normal")
        self.sync_button.configure(state="disabled" if busy else "normal")

    def _start_scan(self) -> None:
        if self.busy:
            return
        report_root = Path(self.report_var.get().strip())
        library_root = Path(self.library_var.get().strip())
        if not report_root.is_dir():
            messagebox.showerror("无法扫描", "请选择有效的 Tag 报告目录。")
            return
        if not str(library_root):
            messagebox.showerror("无法扫描", "请填写目标图片库目录。")
            return
        self._set_busy(True)
        self.status_var.set("正在读取报告并检查原图、目标路径与数据库……")
        self.phase_var.set("移动进度：正在生成计划")

        def worker() -> None:
            try:
                entries = load_latest_successful_tags(report_root)
                self.events.put(("scan_finished", make_plans(entries, library_root)))
            except Exception as exc:
                self.events.put(("error", f"扫描失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _start_move(self) -> None:
        if self.busy:
            return
        workable = [plan for plan in self.plans if plan.state in {"ready", "recovery"}]
        if not workable:
            messagebox.showinfo("没有可移动的文件", "当前计划中没有可安全移动的图片。")
            return
        warning = (
            f"将处理 {len(workable)} 张图片。\n\n"
            "程序会先完整复制并以 SHA-256 校验所有文件，再写入 image_tags.db，最后删除原图。\n"
            "复制或校验任意一张失败时，程序不会删除任何原图。\n\n"
            "确认后，原图将在全部复制验证完成后被删除。是否继续？"
        )
        if not messagebox.askyesno("确认安全移动", warning, icon="warning"):
            return
        self._set_busy(True)
        self.progress.configure(maximum=len(workable) * 3, value=0)
        self.status_var.set("开始批量复制；在所有复制与校验完成前不会删除任何原图。")
        self.phase_var.set(f"移动进度：0/{len(workable) * 3}（0.0%）· 等待复制")
        self.result_var.set("结果：正在执行安全移动，原图暂不会删除。")
        library_root = Path(self.library_var.get().strip())

        def worker() -> None:
            try:
                # execute_safe_move 的回调签名是 (事件名, 数据)；Queue.put 只接受一个事件对象。
                # 显式封装为元组，确保 GUI 能持续接收进度和最终完成事件。
                result = execute_safe_move(
                    workable,
                    library_root,
                    lambda kind, payload: self.events.put((kind, payload)),
                )
                self.events.put(("move_finished", result))
            except TransferError as exc:
                suffix = f"\n临时副本位置：{exc.staging_dir}" if exc.staging_dir else ""
                self.events.put(("error", f"移动未完成：{exc}\n复制/校验阶段未删除原图。{suffix}"))
            except Exception as exc:
                self.events.put(("error", f"移动未完成：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _start_sync(self) -> None:
        if self.busy:
            return
        library_root = Path(self.library_var.get().strip())
        if not str(library_root):
            messagebox.showerror("无法更新", "请填写目标图片库目录。")
            return
        self._set_busy(True)
        self.phase_var.set("移动进度：正在将数据库写入 F 盘缓存并生成 CSV")
        self.status_var.set("正在更新 F 盘缓存，并将数据库和 CSV 作为快照同步到目标图片库……")

        def worker() -> None:
            try:
                self.events.put(("sync_finished", synchronize_cached_database(library_root)))
            except Exception as exc:
                self.events.put(("error", f"数据库/CSV 更新失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        labels = {
            "ready": "准备移动", "recovery": "可恢复移动", "source_missing": "原图缺失",
            "already_moved": "已移动", "already_in_library": "已在图片库", "destination_conflict": "目标冲突",
        }
        # 仅展示前 2,000 条，完整清单仍由数据库和原始报告保存，避免大批量任务卡住界面。
        for plan in self.plans[:2000]:
            self.table.insert("", "end", values=(
                labels.get(plan.state, plan.state), plan.entry.source.name, category_folder(plan.entry.category),
                "、".join(plan.entry.tags[:4]), str(plan.destination or ""), plan.note,
            ))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "scan_finished":
                    self.plans = payload
                    self._fill_table()
                    counts: dict[str, int] = {}
                    for plan in self.plans:
                        counts[plan.state] = counts.get(plan.state, 0) + 1
                    ready = counts.get("ready", 0) + counts.get("recovery", 0)
                    self.summary_var.set(f"共 {len(self.plans)} 条；可移动 {ready} 条")
                    self.status_var.set(
                        f"扫描完成：可移动 {ready} 张，原图缺失 {counts.get('source_missing', 0)} 张，"
                        f"目标冲突 {counts.get('destination_conflict', 0)} 张。"
                    )
                    self.phase_var.set(f"移动进度：计划已生成，可移动 {ready} 张")
                    self._set_busy(False)
                elif kind in {"copy", "promote", "delete"}:
                    index, total, plan = payload
                    phase = {"copy": "复制并校验", "promote": "整理到分类目录", "delete": "删除已校验原图"}[kind]
                    offset = {"copy": 0, "promote": total, "delete": total * 2}[kind]
                    overall = offset + index
                    full_total = total * 3
                    self.progress.configure(maximum=full_total, value=overall)
                    self.status_var.set(f"{phase} {index}/{total}：{plan.entry.source.name}")
                    self.phase_var.set(
                        f"移动进度：{overall}/{full_total}（{overall / full_total * 100:.1f}%）· {phase} {index}/{total}"
                    )
                elif kind == "move_finished":
                    self._set_busy(False)
                    result = payload
                    failed = result["delete_failed"]
                    self.status_var.set(f"处理结束：已移动 {result['moved']} 张；删除失败 {len(failed)} 张。")
                    self.phase_var.set(f"移动进度：全部阶段完成（{result['moved']} 张已移动）")
                    if result["csv_error"]:
                        self.result_var.set(
                            f"结果：图片移动完成，但 CSV 导出失败。SQLite 数据库仍可用：{database_path(Path(self.library_var.get().strip()))}"
                        )
                    else:
                        self.result_var.set(
                            f"结果：已完成 {result['moved']} 张；CSV 标签清单：{result['csv_path']}"
                        )
                    if failed:
                        details = "\n".join(f"{plan.entry.source.name}：{error}" for plan, error in failed[:10])
                        messagebox.showwarning(
                            "复制已完成，但部分原图未删除",
                            f"已移动 {result['moved']} 张。\n{len(failed)} 张原图删除失败，数据库已保留 copied_verified 状态。\n\n{details}",
                        )
                    else:
                        messagebox.showinfo(
                            "安全移动完成",
                            f"已移动 {result['moved']} 张图片。\n\n标签数据库：\n{database_path(Path(self.library_var.get().strip()))}\n\n可读 CSV：\n{result['csv_path']}",
                        )
                    self._start_scan()
                elif kind == "sync_finished":
                    self._set_busy(False)
                    result = payload
                    self.phase_var.set("移动进度：数据库与 CSV 已同步")
                    self.status_var.set(f"F 盘缓存已更新，并已同步数据库和 CSV 到 {Path(result['target_db']).parent}。")
                    self.result_var.set(f"结果：可读 CSV：{result['target_csv']}；缓存目录：{Path(result['cached_db']).parent}")
                    messagebox.showinfo(
                        "数据库与 CSV 已更新",
                        f"F 盘缓存数据库：\n{result['cached_db']}\n\nG 盘 CSV：\n{result['target_csv']}",
                    )
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set(str(payload))
                    self.phase_var.set("移动进度：操作未完成")
                    self.result_var.set(f"结果：{payload}")
                    messagebox.showerror("操作未完成", str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


if __name__ == "__main__":
    TagMoveApp().mainloop()
