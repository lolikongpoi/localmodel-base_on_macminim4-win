from __future__ import annotations

import argparse
import csv
import os
import queue
import shutil
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BOTH, END, LEFT, BooleanVar, StringVar, Tk, filedialog, messagebox, ttk

try:
    from PIL import Image, ImageFile, ImageOps
except ImportError as exc:
    raise SystemExit("缺少 Pillow。请先双击‘安装环境.bat’。") from exc


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = APP_DIR / "分类结果"
MIN_SIZE = 10 * 1024
MEDIUM_SIZE = 100 * 1024
LARGE_SIZE = 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif", ".heic"}
SYSTEM_NAMES = {"thumbs.db", "desktop.ini"}
LOG_LIMIT = 500

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True


@dataclass(frozen=True)
class ClassifiedFile:
    source: Path
    category: str
    size_bytes: int
    width: int | None
    height: int | None
    image_format: str
    note: str
    operation: str = ""

    @property
    def needs_png_conversion(self) -> bool:
        """QQ cache files without a suffix are exported as real, named PNG files."""
        return not self.source.suffix and self.width is not None


def discover_qq_cache() -> Path | None:
    """Find the most recently changed legacy QQ group-image cache, if present."""
    documents = Path.home() / "Documents" / "Tencent Files"
    if not documents.is_dir():
        return None
    candidates: list[Path] = []
    for account in documents.iterdir():
        if not account.is_dir():
            continue
        image_root = account / "Image"
        for name in ("Group2", "Group"):
            folder = image_root / name
            if folder.is_dir():
                candidates.append(folder)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def iter_candidates(root: Path, recursive: bool = True):
    """Yield likely image files, including extensionless cache files that Pillow can verify."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            if path.name.lower() not in SYSTEM_NAMES and (path.suffix.lower() in IMAGE_EXTENSIONS or not path.suffix):
                                yield path
                    except OSError:
                        continue
        except OSError:
            continue


def image_info(path: Path) -> tuple[int | None, int | None, str, str]:
    try:
        with Image.open(path) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            return width, height, image.format or "", ""
    except Exception as exc:  # A cache can contain partial or no-longer-valid files.
        return None, None, "", type(exc).__name__


def category_for(size_bytes: int, width: int | None, height: int | None, error: str) -> tuple[str, str]:
    # The order intentionally follows the original project: geometry rules take priority over size.
    if size_bytes <= MIN_SIZE:
        return "00_小于10KB", "原项目会删除；此程序保留并单独归档"
    if error or not width or not height:
        return "99_无法读取", error or "没有图片尺寸"
    ratio = max(width / height, height / width)
    if ratio > 4.5:
        return "01_长图", "长宽比大于 4.5"
    if width == 1080 or height == 1080:
        return "02_含1080边", "可能是手机截图或 1080P 图片"
    dimensions = tuple(sorted((width, height)))
    # Sorting dimensions means the portrait and landscape variants share one rule.
    if dimensions == (1440, 2160):
        return "03_2K", "1440 × 2160（横竖方向均识别）"
    if dimensions == (1440, 2560):
        return "03_2K", "2560 × 1440 / 1440 × 2560"
    if dimensions == (2160, 3840):
        return "04_4K", "3840 × 2160 / 2160 × 3840"
    if size_bytes > LARGE_SIZE:
        return "10_大于1MB", "按文件大小"
    if size_bytes > MEDIUM_SIZE:
        return "11_100KB至1MB", "按文件大小"
    return "12_10KB至100KB", "按文件大小"


def classify(path: Path) -> ClassifiedFile:
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        return ClassifiedFile(path, "99_无法读取", 0, None, None, "", type(exc).__name__)
    width, height, image_format, error = image_info(path)
    category, note = category_for(size_bytes, width, height, error)
    if not path.suffix and not error:
        note = f"{note}；无后缀，复制或移动时重新编码为 PNG"
    return ClassifiedFile(path, category, size_bytes, width, height, image_format, note)


def output_filename(item: ClassifiedFile) -> str:
    return f"{item.source.name}.png" if item.needs_png_conversion else item.source.name


def unique_destination(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    source = Path(filename)
    stem, suffix = source.stem, source.suffix
    index = 2
    while True:
        target = folder / f"{stem} ({index}){suffix}"
        if not target.exists():
            return target
        index += 1


def save_as_png(source: Path, destination: Path) -> None:
    """Decode first, then save a portable PNG; never rename unverified cache bytes."""
    with Image.open(source) as image:
        image.seek(0)
        image = ImageOps.exif_transpose(image)
        has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        image.save(destination, format="PNG")


def write_report(items: list[ClassifiedFile], output: Path, action: str) -> Path:
    reports = output / "报告"
    reports.mkdir(parents=True, exist_ok=True)
    report = reports / f"预分类清单_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["源文件", "类别", "文件大小(B)", "宽", "高", "格式", "无后缀转PNG", "说明", "操作"])
        for item in items:
            writer.writerow([item.source, item.category, item.size_bytes, item.width or "", item.height or "", item.image_format, "是" if item.needs_png_conversion else "否", item.note, item.operation or action])
    return report


def run_preclassification(
    source: Path,
    output: Path,
    action: str,
    recursive: bool,
    progress=None,
    resume_event: threading.Event | None = None,
    stop_event: threading.Event | None = None,
    discard_small: bool = False,
) -> tuple[list[ClassifiedFile], Path, bool]:
    paths = list(iter_candidates(source, recursive))
    items: list[ClassifiedFile] = []
    total = len(paths)
    for index, path in enumerate(paths, 1):
        # Controls take effect between files, so a copied/converted file is never left half-written.
        while resume_event is not None and not resume_event.wait(0.2):
            if stop_event is not None and stop_event.is_set():
                break
        if stop_event is not None and stop_event.is_set():
            break
        item = classify(path)
        if action != "preview":
            if discard_small and action == "move" and item.size_bytes <= MIN_SIZE:
                path.unlink()
                item = replace(item, operation="删除（≤10KB）")
            else:
                target_folder = output / item.category
                target_folder.mkdir(parents=True, exist_ok=True)
                target = unique_destination(target_folder, output_filename(item))
                if item.needs_png_conversion:
                    save_as_png(path, target)
                    if action == "move":
                        path.unlink()
                    item = replace(item, operation="移动并转PNG" if action == "move" else "复制并转PNG")
                elif action == "copy":
                    shutil.copy2(path, target)
                    item = replace(item, operation="复制")
                elif action == "move":
                    shutil.move(str(path), str(target))
                    item = replace(item, operation="移动")
        items.append(item)
        if progress:
            progress(index, total, item)
    stopped = (stop_event is not None and stop_event.is_set()) or len(items) < total
    action_label = {"preview": "仅预览", "copy": "复制", "move": "移动"}[action]
    if stopped:
        action_label = f"{action_label}（已停止）"
    report = write_report(items, output, action_label)
    return items, report, stopped


def format_summary(items: list[ClassifiedFile], report: Path, stopped: bool = False) -> str:
    counts = Counter(item.category for item in items)
    prefix = "已停止" if stopped else "完成"
    lines = [f"{prefix}：共处理 {len(items)} 个候选文件。"]
    lines.extend(f"{name}：{count}" for name, count in sorted(counts.items()))
    lines.append(f"报告：{report}")
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class PreclassifierApp:
    def __init__(self, root: Tk):
        self.root = root
        root.title("QQ 群聊图片缓存预分类")
        root.minsize(780, 520)
        default_source = discover_qq_cache()
        self.source_var = StringVar(value=str(default_source) if default_source else "")
        self.output_var = StringVar(value=str(DEFAULT_OUTPUT))
        self.action_var = StringVar(value="preview")
        self.recursive_var = BooleanVar(value=True)
        self.discard_small_var = BooleanVar(value=False)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.paused = False
        self.resume_event = threading.Event()
        self.stop_event = threading.Event()
        self.started_at: datetime | None = None
        self.started_monotonic: float | None = None
        self.pause_started_monotonic: float | None = None
        self.paused_seconds = 0.0
        self.current_done = 0
        self.current_total = 0
        self._build()
        root.after(100, self._poll_events)

    def _build(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill=BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="缓存文件夹").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="选择…", command=self._choose_source).grid(row=0, column=2)
        ttk.Label(frame, text="输出文件夹").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="选择…", command=self._choose_output).grid(row=1, column=2)
        ttk.Label(frame, text="执行方式").grid(row=2, column=0, sticky="w", pady=4)
        action = ttk.Combobox(frame, textvariable=self.action_var, state="readonly", values=("preview", "copy", "move"), width=14)
        action.grid(row=2, column=1, sticky="w", padx=8)
        action.bind("<<ComboboxSelected>>", self._update_discard_state)
        ttk.Label(frame, text="preview 仅生成清单；copy 默认安全；move 会移动缓存文件。", foreground="#555").grid(row=3, column=1, sticky="w", padx=8)
        self.discard_small_checkbox = ttk.Checkbutton(frame, text="移动时直接丢弃（删除）≤10KB 文件", variable=self.discard_small_var)
        self.discard_small_checkbox.grid(row=4, column=1, sticky="w", padx=8, pady=(4, 0))
        ttk.Checkbutton(frame, text="递归扫描子文件夹", variable=self.recursive_var).grid(row=5, column=1, sticky="w", padx=8, pady=4)
        self.start_button = ttk.Button(frame, text="开始预分类", command=self._start)
        self.start_button.grid(row=6, column=1, sticky="w", padx=8, pady=10)
        controls = ttk.Frame(frame)
        controls.grid(row=6, column=1, sticky="e", padx=8, pady=10)
        self.pause_button = ttk.Button(controls, text="暂停", command=self._toggle_pause, state="disabled")
        self.pause_button.pack(side=LEFT, padx=(0, 6))
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side=LEFT)
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.status_var = StringVar(value="请选择 QQ 群聊图片缓存文件夹；程序也会尝试自动填入旧版 QQ 路径。")
        ttk.Label(frame, textvariable=self.status_var).grid(row=8, column=0, columnspan=3, sticky="w")
        self.time_var = StringVar(value="已用时间：00:00:00；预计完成：等待开始")
        ttk.Label(frame, textvariable=self.time_var, foreground="#555").grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.log = ttk.Treeview(frame, columns=("category", "file", "size", "note"), show="headings", height=16)
        for column, title, width in (("category", "分类", 130), ("file", "文件", 330), ("size", "大小", 100), ("note", "说明", 180)):
            self.log.heading(column, text=title)
            self.log.column(column, width=width, anchor="w")
        log_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        log_scroll.grid(row=10, column=2, sticky="ns", pady=(8, 0))
        frame.rowconfigure(10, weight=1)
        self._update_discard_state()

    def _choose_source(self):
        folder = filedialog.askdirectory(title="选择 QQ 群聊图片缓存文件夹", initialdir=self.source_var.get() or str(Path.home()))
        if folder:
            self.source_var.set(folder)

    def _choose_output(self):
        folder = filedialog.askdirectory(title="选择分类结果输出文件夹", initialdir=self.output_var.get() or str(DEFAULT_OUTPUT))
        if folder:
            self.output_var.set(folder)

    def _update_discard_state(self, _event=None):
        if self.action_var.get() == "move":
            self.discard_small_checkbox.configure(state="normal")
        else:
            self.discard_small_var.set(False)
            self.discard_small_checkbox.configure(state="disabled")

    def _start(self):
        if self.running:
            return
        source = Path(self.source_var.get().strip())
        output = Path(self.output_var.get().strip())
        if not source.is_dir():
            messagebox.showerror("文件夹不存在", "请选择一个存在的 QQ 图片缓存文件夹。")
            return
        if source.resolve() == output.resolve() or source in output.parents:
            messagebox.showerror("输出位置不安全", "输出文件夹不能是缓存文件夹本身或其上级文件夹。")
            return
        if self.action_var.get() == "move" and not messagebox.askyesno("确认移动", "移动会改变 QQ 缓存中的文件位置。确认继续吗？"):
            return
        if self.discard_small_var.get() and not messagebox.askyesno("确认永久删除", "已勾选直接丢弃 ≤10KB 文件。此操作会永久删除这些源文件，不能撤销。确认继续吗？"):
            return
        self.running = True
        self.paused = False
        self.resume_event.set()
        self.stop_event.clear()
        self.started_at = datetime.now()
        self.started_monotonic = time.monotonic()
        self.pause_started_monotonic = None
        self.paused_seconds = 0.0
        self.current_done = 0
        self.current_total = 0
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="暂停")
        self.stop_button.configure(state="normal")
        self.log.delete(*self.log.get_children())
        self.status_var.set("正在扫描并处理…")
        threading.Thread(
            target=self._work,
            args=(source, output, self.action_var.get(), self.recursive_var.get(), self.discard_small_var.get()),
            daemon=True,
        ).start()
        self._refresh_timing()

    def _toggle_pause(self):
        if not self.running or self.stop_event.is_set():
            return
        self.paused = not self.paused
        if self.paused:
            self.pause_started_monotonic = time.monotonic()
            self.resume_event.clear()
            self.pause_button.configure(text="继续")
            self.status_var.set("已暂停；点击“继续”后将从下一张图片继续。")
        else:
            if self.pause_started_monotonic is not None:
                self.paused_seconds += time.monotonic() - self.pause_started_monotonic
                self.pause_started_monotonic = None
            self.resume_event.set()
            self.pause_button.configure(text="暂停")
            self.status_var.set("正在继续处理…")

    def _stop(self):
        if not self.running or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.resume_event.set()
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止；当前文件完成后会生成部分处理报告。")

    def _work(self, source: Path, output: Path, action: str, recursive: bool, discard_small: bool):
        def progress(done: int, total: int, item: ClassifiedFile):
            if done <= 200 or done % 50 == 0 or done == total:
                self.events.put(("progress", (done, total, item)))
        try:
            items, report, stopped = run_preclassification(source, output, action, recursive, progress, self.resume_event, self.stop_event, discard_small)
            self.events.put(("done", (items, report, stopped)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    done, total, item = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.current_done, self.current_total = done, total
                    self.status_var.set(f"处理中：{done}/{total}  {item.source.name}")
                    children = self.log.get_children()
                    if len(children) >= LOG_LIMIT:
                        for row in children[: len(children) - LOG_LIMIT + 1]:
                            self.log.delete(row)
                    self.log.insert("", END, values=(item.category, item.source.name, f"{item.size_bytes / 1024:.1f} KB", item.note))
                    self.log.yview_moveto(1)
                elif event == "done":
                    items, report, stopped = payload
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.stop_button.configure(state="disabled")
                    summary = format_summary(items, report, stopped)
                    self.status_var.set(summary.splitlines()[0])
                    self._refresh_timing(final=True)
                    messagebox.showinfo("预分类已停止" if stopped else "预分类完成", summary)
                elif event == "error":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.stop_button.configure(state="disabled")
                    self.status_var.set("处理失败")
                    messagebox.showerror("处理失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _refresh_timing(self, final: bool = False):
        if self.started_monotonic is None:
            return
        now = time.monotonic()
        paused_now = now - self.pause_started_monotonic if self.pause_started_monotonic is not None else 0.0
        active_elapsed = max(0.0, now - self.started_monotonic - self.paused_seconds - paused_now)
        text = f"已用时间：{format_duration(active_elapsed)}"
        if self.current_done > 0 and self.current_total >= self.current_done:
            remaining_seconds = active_elapsed / self.current_done * (self.current_total - self.current_done)
            completion = datetime.now() + timedelta(seconds=remaining_seconds)
            text += f"；预计完成：{completion:%H:%M:%S}；剩余：{format_duration(remaining_seconds)}"
        else:
            text += "；预计完成：正在扫描…"
        if self.paused:
            text += "（已暂停）"
        self.time_var.set(text)
        if self.running and not final:
            self.root.after(500, self._refresh_timing)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按原 QQ 缓存整理脚本的规则安全预分类图片")
    parser.add_argument("source", nargs="?", type=Path, help="QQ 群聊缓存目录")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="分类结果目录")
    parser.add_argument("--action", choices=("preview", "copy", "move"), default="preview")
    parser.add_argument("--discard-small", action="store_true", help="仅在 move 模式下永久删除 ≤10KB 文件")
    parser.add_argument("--no-recursive", action="store_true", help="只扫描当前目录")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.gui or args.source is None:
        root = Tk()
        PreclassifierApp(root)
        root.mainloop()
        return
    if not args.source.is_dir():
        raise SystemExit(f"缓存文件夹不存在：{args.source}")
    if args.discard_small and args.action != "move":
        raise SystemExit("--discard-small 只能与 --action move 一起使用")
    started = time.monotonic()
    def progress(done: int, total: int, item: ClassifiedFile):
        if done == total or done % 100 == 0:
            print(f"{done}/{total}  {item.source.name}", flush=True)
    items, report, stopped = run_preclassification(args.source, args.output, args.action, not args.no_recursive, progress, discard_small=args.discard_small)
    print(format_summary(items, report, stopped))
    print(f"耗时：{time.monotonic() - started:.1f} 秒")


if __name__ == "__main__":
    main()
