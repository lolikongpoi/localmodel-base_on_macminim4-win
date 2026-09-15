from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mac_tag_client import (
    IncrementalReportWriter,
    MacTagService,
    TagRecord,
    find_completed_tag_sources,
    normalize_source_path,
    scan_images,
    tag_with_single_retry,
)


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_VERSION = 1


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "计算中"
    seconds = round(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"约 {hours} 小时 {minutes} 分"
    if minutes:
        return f"约 {minutes} 分 {seconds} 秒"
    return f"约 {seconds} 秒"


class MacTagApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Mac 局域网图片 Tag 客户端")
        self.geometry("1240x760")
        self.minsize(960, 620)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.busy = False
        self.records: list[TagRecord] = []
        self.records_by_item: dict[str, TagRecord] = {}
        self.started_at = 0.0
        self.paused_at: float | None = None
        self.paused_seconds = 0.0
        self.last_eta_seconds: float | None = None
        self.last_time_limit_seconds: float | None = None
        self._build_ui()
        self.after(100, self._poll_events)
        self.after(250, self._update_clock)

    def _build_ui(self) -> None:
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.folder_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(PROJECT_DIR / "mac_tag_reports"))
        self.url_var = tk.StringVar(value=os.environ.get("IMAGE_TAGGER_URL", "http://"))
        self.key_var = tk.StringVar(value=os.environ.get("IMAGE_TAGGER_API_KEY", ""))
        self.preset_var = tk.StringVar(value="4b")
        self.limit_var = tk.StringVar()
        self.duration_var = tk.StringVar()
        self.folder_count_var = tk.StringVar(value="未统计")
        self.tag_check_var = tk.StringVar(value="未检查已完成 Tag")
        self.elapsed_var = tk.StringVar(value="已用时间：00:00:00")
        self.finish_time_var = tk.StringVar(value="预计完成：等待首张图片")
        self.recursive_var = tk.BooleanVar(value=True)
        self.skip_completed_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="填写 Mac 局域网地址和 API key 后，先检查联通。")

        self._row(form, 0, "图片文件夹", self.folder_var, self._choose_folder)
        self._row(form, 1, "报告保存位置", self.output_var, self._choose_output)
        ttk.Label(form, text="Mac 服务地址").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.url_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="例如 http://192.168.1.23:8787").grid(row=2, column=2, sticky="w", padx=(8, 0))
        ttk.Label(form, text="API key").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.key_var, show="●").grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="只存在本次运行内存，不会写入报告或配置文件").grid(row=3, column=2, sticky="w", padx=(8, 0))

        limits = ttk.Frame(form)
        limits.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(limits, text="最多处理图片数：").pack(side="left")
        ttk.Entry(limits, textvariable=self.limit_var, width=9).pack(side="left", padx=(4, 16))
        ttk.Label(limits, text="留空=全部").pack(side="left")
        ttk.Label(limits, text="最长运行时间（分钟）：").pack(side="left", padx=(22, 0))
        ttk.Entry(limits, textvariable=self.duration_var, width=9).pack(side="left", padx=(4, 16))
        ttk.Label(limits, text="留空=不限；暂停期间不计时").pack(side="left")
        batch_actions = ttk.Frame(limits)
        batch_actions.pack(side="right")
        self.start_button = ttk.Button(batch_actions, text="开始批量打 Tag", command=self._start_tagging)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(batch_actions, text="暂停（当前图片完成后）", command=self._toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(batch_actions, text="停止（当前图片完成后）", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))

        actions = ttk.Frame(form)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.health_button = ttk.Button(actions, text="检查联通", command=self._start_health)
        self.health_button.pack(side="left")
        self.models_button = ttk.Button(actions, text="读取模型", command=self._start_models)
        self.models_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="模型预设：").pack(side="left", padx=(20, 0))
        self.preset_box = ttk.Combobox(actions, textvariable=self.preset_var, values=["4b", "8b"], width=5, state="readonly")
        self.preset_box.pack(side="left", padx=(4, 0))
        self.switch_button = ttk.Button(actions, text="切换模型", command=self._start_switch)
        self.switch_button.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(actions, text="递归扫描子文件夹", variable=self.recursive_var).pack(side="left", padx=(20, 0))
        self.count_button = ttk.Button(actions, text="统计图片数量", command=self._start_count)
        self.count_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.folder_count_var).pack(side="left", padx=(6, 0))
        self.completed_check_button = ttk.Button(actions, text="检查已完成 Tag", command=self._start_completed_check)
        self.completed_check_button.pack(side="left", padx=(12, 0))
        ttk.Label(actions, textvariable=self.tag_check_var).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="保存配置…", command=self._save_config).pack(side="left", padx=(16, 0))
        ttk.Button(actions, text="读取配置…", command=self._load_config).pack(side="left", padx=(6, 0))

        shortcuts = ttk.Frame(form)
        shortcuts.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(shortcuts, text="跳过已完成 Tag 的图片", variable=self.skip_completed_var).pack(side="left")
        self.open_photo_button = ttk.Button(shortcuts, text="打开选中照片", command=self._open_selected_photo)
        self.open_photo_button.pack(side="right")
        self.open_explorer_button = ttk.Button(shortcuts, text="资源管理器中显示", command=self._show_selected_in_explorer)
        self.open_explorer_button.pack(side="right", padx=(0, 8))

        info = ttk.Frame(self, padding=(12, 0, 12, 6))
        info.pack(fill="x")
        ttk.Label(info, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(info, mode="determinate", length=300)
        self.progress.pack(side="right")

        timing = ttk.Frame(self, padding=(12, 0, 12, 6))
        timing.pack(fill="x")
        ttk.Label(timing, textvariable=self.elapsed_var).pack(side="left")
        ttk.Label(timing, textvariable=self.finish_time_var).pack(side="left", padx=(22, 0))

        table_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        table_frame.pack(fill="both", expand=True)
        columns = ("file", "status", "category", "tags", "character", "summary", "seconds")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        labels = {"file": "文件", "status": "状态", "category": "主类别", "tags": "Tags", "character": "角色候选（≥0.80）", "summary": "摘要", "seconds": "耗时"}
        widths = {"file": 260, "status": 76, "category": 130, "tags": 250, "character": 150, "summary": 300, "seconds": 70}
        for column in columns:
            self.table.heading(column, text=labels[column])
            self.table.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<Double-1>", lambda _event: self._open_selected_photo())
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command: callable) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择…", command=command).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)

    def _choose_folder(self) -> None:
        value = filedialog.askdirectory(title="选择需要打 Tag 的图片文件夹")
        if value:
            self.folder_var.set(value)

    def _choose_output(self) -> None:
        value = filedialog.askdirectory(title="选择报告保存位置")
        if value:
            self.output_var.set(value)

    def _service(self) -> MacTagService:
        return MacTagService(self.url_var.get(), self.key_var.get())

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.health_button,
            self.models_button,
            self.switch_button,
            self.count_button,
            self.completed_check_button,
            self.start_button,
        ):
            button.configure(state=state)
        self.preset_box.configure(state="disabled" if busy else "readonly")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.pause_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.pause_button.configure(text="暂停（当前图片完成后）")

    @staticmethod
    def _positive_number(value: str, field_name: str, integer: bool = False) -> float | int | None:
        value = value.strip()
        if not value:
            return None
        try:
            parsed = int(value) if integer else float(value)
        except ValueError as exc:
            raise ValueError(f"{field_name}必须是正数") from exc
        if parsed <= 0:
            raise ValueError(f"{field_name}必须大于 0")
        return parsed

    def _run_request(self, kind: str, action: callable) -> None:
        if self.busy:
            return
        try:
            service = self._service()
        except Exception as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        self._set_busy(True)
        self.pause_button.configure(state="disabled")
        self.status_var.set("正在请求 Mac 服务……")
        def worker() -> None:
            try:
                self.events.put((kind, action(service)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _start_health(self) -> None:
        self._run_request("health", lambda service: service.health())

    def _start_models(self) -> None:
        self._run_request("models", lambda service: service.models())

    def _start_switch(self) -> None:
        preset = self.preset_var.get()
        if not messagebox.askyesno("确认切换模型", f"将切换 Mac 上的模型到 {preset}。首次下载可能超过十分钟，期间服务不能识别图片。是否继续？"):
            return
        self._run_request("switch", lambda service: service.switch_model(preset))

    def _start_count(self) -> None:
        if self.busy:
            return
        folder = Path(self.folder_var.get().strip())
        if not folder.is_dir():
            messagebox.showerror("无法统计", "请选择有效的图片文件夹")
            return
        recursive = self.recursive_var.get()
        self.count_button.configure(state="disabled")
        self.folder_count_var.set("正在统计…")

        def worker() -> None:
            try:
                count = len(scan_images(folder, recursive))
                self.events.put(("folder_count", count))
            except Exception as exc:
                self.events.put(("count_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_completed_check(self) -> None:
        if self.busy:
            return
        folder = Path(self.folder_var.get().strip())
        output = Path(self.output_var.get().strip())
        if not folder.is_dir():
            messagebox.showerror("无法检查", "请选择有效的图片文件夹")
            return
        self.completed_check_button.configure(state="disabled")
        self.tag_check_var.set("正在检查…")

        def worker() -> None:
            try:
                paths = scan_images(folder, self.recursive_var.get())
                completed = find_completed_tag_sources(output)
                done = sum(normalize_source_path(path) in completed for path in paths)
                self.events.put(("completed_check", (len(paths), done)))
            except Exception as exc:
                self.events.put(("completed_check_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _config_data(self) -> dict[str, object]:
        return {
            "version": CONFIG_VERSION,
            "folder": self.folder_var.get().strip(),
            "output_folder": self.output_var.get().strip(),
            "service_url": self.url_var.get().strip(),
            "preset": self.preset_var.get(),
            "recursive": self.recursive_var.get(),
            "max_images": self.limit_var.get().strip(),
            "max_minutes": self.duration_var.get().strip(),
        }

    def _save_config(self) -> None:
        destination = filedialog.asksaveasfilename(
            title="保存 Tag 任务配置",
            defaultextension=".json",
            filetypes=[("JSON 配置", "*.json")],
            initialfile="mac_tag_task_config.json",
        )
        if not destination:
            return
        try:
            Path(destination).write_text(
                json.dumps(self._config_data(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.status_var.set(f"配置已保存至 {destination}。API key 未保存。")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_config(self) -> None:
        source = filedialog.askopenfilename(
            title="读取 Tag 任务配置", filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")]
        )
        if not source:
            return
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置文件必须是 JSON 对象")
            self.folder_var.set(str(data.get("folder", "")))
            self.output_var.set(str(data.get("output_folder", str(PROJECT_DIR / "mac_tag_reports"))))
            self.url_var.set(str(data.get("service_url", "http://")))
            preset = str(data.get("preset", "4b"))
            self.preset_var.set(preset if preset in {"4b", "8b"} else "4b")
            self.recursive_var.set(bool(data.get("recursive", True)))
            self.limit_var.set(str(data.get("max_images", "")))
            self.duration_var.set(str(data.get("max_minutes", "")))
            self.key_var.set("")
            self.folder_count_var.set("未统计")
            self.status_var.set("配置已读取。为安全起见，API key 未保存，请重新输入。")
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取配置：{exc}")

    def _start_tagging(self) -> None:
        try:
            folder = Path(self.folder_var.get().strip())
            output = Path(self.output_var.get().strip())
            if not folder.is_dir():
                raise ValueError("请选择有效的图片文件夹")
            if not str(output):
                raise ValueError("请选择报告保存位置")
            service = self._service()
            paths = scan_images(folder, self.recursive_var.get())
            if not paths:
                raise ValueError("此文件夹中没有支持的图片")
            limit = self._positive_number(self.limit_var.get(), "最多处理图片数", integer=True)
            duration_minutes = self._positive_number(self.duration_var.get(), "最长运行时间")
            completed_count = 0
            if self.skip_completed_var.get():
                completed = find_completed_tag_sources(output)
                original_count = len(paths)
                paths = [path for path in paths if normalize_source_path(path) not in completed]
                completed_count = original_count - len(paths)
            if limit is not None:
                paths = paths[:int(limit)]
            if not paths:
                if completed_count:
                    raise ValueError("扫描范围内的图片都已有成功 Tag 记录。可取消“跳过已完成 Tag 的图片”后重新处理。")
                raise ValueError("没有可处理的图片")
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        self.cancel_event.clear()
        self.pause_event.set()
        self.records = []
        self.records_by_item = {}
        self.started_at = time.monotonic()
        self.paused_at = None
        self.paused_seconds = 0.0
        self.last_eta_seconds = None
        self.last_time_limit_seconds = None if duration_minutes is None else float(duration_minutes) * 60
        self.elapsed_var.set("已用时间：00:00:00")
        self.finish_time_var.set("预计完成：等待首张图片")
        self.progress.configure(maximum=len(paths), value=0)
        for item in self.table.get_children():
            self.table.delete(item)
        self._set_busy(True)
        detail = []
        if limit is not None:
            detail.append(f"最多 {len(paths)} 张")
        if duration_minutes is not None:
            detail.append(f"最长 {duration_minutes:g} 分钟")
        if completed_count:
            detail.append(f"跳过已完成 {completed_count} 张")
        self.status_var.set(f"正在检查 Mac 服务并准备处理 {len(paths)} 张图片……{'；'.join(detail)}")
        threading.Thread(
            target=self._tag_worker,
            args=(service, folder, output, paths, None if duration_minutes is None else float(duration_minutes) * 60),
            daemon=True,
        ).start()

    def _tag_worker(
        self, service: MacTagService, folder: Path, output: Path, paths: list[Path], max_seconds: float | None
    ) -> None:
        writer: IncrementalReportWriter | None = None
        stop_reason = "completed"
        run_dir: Path | None = None
        try:
            writer = IncrementalReportWriter(output, folder, service.base_url)
            health = service.health()
            if health.get("status") != "ok":
                raise RuntimeError(f"Mac 服务健康检查未通过：{health}")
            self.events.put(("model_status", health))
            deadline = time.monotonic() + max_seconds if max_seconds is not None else None
            pause_started: float | None = None
            pause_announced = False
            for index, path in enumerate(paths, start=1):
                if self.cancel_event.is_set():
                    stop_reason = "user_stop"
                    break
                while not self.pause_event.is_set():
                    if self.cancel_event.is_set():
                        stop_reason = "user_stop"
                        break
                    if not pause_announced:
                        pause_started = time.monotonic()
                        pause_announced = True
                        self.events.put(("paused", None))
                    time.sleep(0.25)
                if self.cancel_event.is_set():
                    break
                if pause_announced:
                    if deadline is not None and pause_started is not None:
                        deadline += time.monotonic() - pause_started
                    pause_started = None
                    pause_announced = False
                    self.events.put(("resumed", None))
                if deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "time_limit"
                    break
                self.events.put(("current", (index, len(paths), path.name)))
                record = tag_with_single_retry(service, path)
                self.records.append(record)
                assert writer is not None
                writer.append(record)
                average = sum(item.elapsed_seconds for item in self.records) / index
                eta = average * (len(paths) - index)
                if deadline is not None:
                    eta = min(eta, max(0.0, deadline - time.monotonic()))
                self.events.put(("record", (index, len(paths), record, eta)))
            run_dir = writer.finish(planned_total=len(paths), stop_reason=stop_reason)
            self.events.put(("finished", (run_dir, stop_reason, len(paths), len(self.records))))
        except Exception as exc:
            if writer is not None:
                try:
                    run_dir = writer.finish(planned_total=len(paths), stop_reason="error")
                except Exception:
                    run_dir = writer.run_dir
            self.events.put(("error", (str(exc), run_dir)))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("收到停止请求：当前图片完成后结束；已完成结果已即时保存。")
        self.cancel_button.configure(state="disabled")

    def _toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text="继续")
            self.status_var.set("暂停请求已发送：当前图片完成后暂停；已完成结果已即时保存。")
        else:
            self.pause_event.set()
            self.pause_button.configure(text="暂停（当前图片完成后）")

    def _add_record(self, record: TagRecord) -> None:
        item = self.table.insert("", "end", values=(
            Path(record.source).name, "完成" if record.status == "success" else "失败", record.category,
            "、".join(record.tags or []), record.auto_character, record.summary,
            f"{record.elapsed_seconds:.1f}s",
        ))
        self.records_by_item[item] = record

    def _selected_record(self) -> TagRecord | None:
        selection = self.table.selection()
        if not selection:
            messagebox.showinfo("请选择照片", "请先在结果列表中选中一张已处理的照片。")
            return None
        return self.records_by_item.get(selection[0])

    def _open_selected_photo(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        path = Path(record.source)
        if not path.is_file():
            messagebox.showerror("文件不存在", f"找不到原图：\n{path}")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("无法打开照片", str(exc))

    def _show_selected_in_explorer(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        path = Path(record.source)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到原图：\n{path}")
            return
        try:
            subprocess.Popen(["explorer.exe", f"/select,{path}"], close_fds=True)
        except OSError as exc:
            messagebox.showerror("无法打开资源管理器", str(exc))

    def _active_elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        now = time.monotonic()
        current_pause = now - self.paused_at if self.paused_at is not None else 0.0
        return max(0.0, now - self.started_at - self.paused_seconds - current_pause)

    def _update_clock(self) -> None:
        if self.started_at:
            elapsed = self._active_elapsed_seconds()
            self.elapsed_var.set(f"已用时间：{format_duration(elapsed).replace('约 ', '')}")
            if self.busy and self.paused_at is not None:
                if self.last_eta_seconds is None:
                    self.finish_time_var.set("预计完成：已暂停，恢复后计算")
                else:
                    self.finish_time_var.set(f"预计完成：已暂停，恢复后约 {format_duration(self.last_eta_seconds)}")
            elif self.busy and self.last_eta_seconds is not None:
                finish_at = datetime.now().timestamp() + self.last_eta_seconds
                self.finish_time_var.set(
                    f"预计完成：{datetime.fromtimestamp(finish_at).strftime('%Y-%m-%d %H:%M:%S')}"
                )
        self.after(250, self._update_clock)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "health":
                    self._set_busy(False)
                    health = payload
                    self.status_var.set(f"联通成功：模型 {health.get('model', '未知')}。")
                elif kind == "models":
                    self._set_busy(False)
                    models = payload
                    current = models.get("current_model", "未知")
                    presets = models.get("presets", {})
                    self.status_var.set(f"当前模型：{current}；可选预设：{', '.join(presets.keys()) or '未返回'}。")
                elif kind == "switch":
                    self._set_busy(False)
                    response = payload
                    self.status_var.set(f"模型切换完成：{response.get('model', response)}")
                elif kind == "model_status":
                    health = payload
                    self.status_var.set(f"Mac 服务已就绪，模型：{health.get('model', '未知')}。")
                elif kind == "current":
                    index, total, name = payload
                    self.status_var.set(f"正在处理 {index}/{total}：{name}")
                elif kind == "record":
                    index, total, record, eta = payload
                    self._add_record(record)
                    self.progress.configure(value=index)
                    self.last_eta_seconds = eta
                    self.status_var.set(f"已完成 {index}/{total}；剩余时间 {format_duration(eta)}。")
                elif kind == "folder_count":
                    if not self.busy:
                        self.count_button.configure(state="normal")
                    self.folder_count_var.set(f"共 {payload} 张")
                    self.status_var.set(f"当前文件夹中共找到 {payload} 张支持的图片。")
                elif kind == "count_error":
                    self.count_button.configure(state="normal")
                    self.folder_count_var.set("统计失败")
                    messagebox.showerror("统计失败", str(payload))
                elif kind == "completed_check":
                    total, completed = payload
                    self.completed_check_button.configure(state="normal")
                    self.tag_check_var.set(f"已完成 {completed} 张，待处理 {total - completed} 张")
                    self.status_var.set(f"已检查 {total} 张图片：已有成功 Tag {completed} 张，待处理 {total - completed} 张。")
                elif kind == "completed_check_error":
                    self.completed_check_button.configure(state="normal")
                    self.tag_check_var.set("检查失败")
                    messagebox.showerror("检查失败", str(payload))
                elif kind == "paused":
                    self.paused_at = time.monotonic()
                    self.status_var.set("任务已暂停；已完成结果已即时写入报告。暂停期间不计入最长运行时间。")
                elif kind == "resumed":
                    if self.paused_at is not None:
                        self.paused_seconds += time.monotonic() - self.paused_at
                        self.paused_at = None
                    self.status_var.set("任务已继续处理。")
                elif kind == "finished":
                    run_dir, reason, total, completed = payload
                    if self.paused_at is not None:
                        self.paused_seconds += time.monotonic() - self.paused_at
                        self.paused_at = None
                    self._set_busy(False)
                    self.last_eta_seconds = 0.0
                    self.finish_time_var.set(f"预计完成：已于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 结束")
                    reason_text = {"completed": "处理完成", "user_stop": "已手动停止", "time_limit": "已达到运行时间上限"}.get(reason, str(reason))
                    self.status_var.set(f"{reason_text}：{completed}/{total} 张。报告已保存至 {run_dir}")
                    messagebox.showinfo("处理结束", f"{reason_text}。\n已处理 {completed}/{total} 张图片。\n报告目录：\n{run_dir}")
                elif kind == "error":
                    if self.paused_at is not None:
                        self.paused_seconds += time.monotonic() - self.paused_at
                        self.paused_at = None
                    self._set_busy(False)
                    error, run_dir = payload if isinstance(payload, tuple) else (str(payload), None)
                    self.status_var.set(f"执行失败；已完成结果已保存至 {run_dir}。" if run_dir else "执行失败。")
                    messagebox.showerror("请求失败", f"{error}\n\n已完成结果目录：\n{run_dir}" if run_dir else error)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


if __name__ == "__main__":
    MacTagApp().mainloop()
