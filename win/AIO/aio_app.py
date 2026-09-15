from __future__ import annotations

"""A small control centre which calls, but never alters, the three existing tools."""

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import ModuleType


AIO_DIR = Path(__file__).resolve().parent
ROOT_DIR = AIO_DIR.parent


def workflow_dir(*names: str) -> Path:
    """Support both the original Chinese folder names and the repository layout."""
    for name in names:
        candidate = ROOT_DIR / name
        if candidate.is_dir():
            return candidate
    return ROOT_DIR / names[0]


MAC_DIR = workflow_dir("通过mac识别", "tagging_by_mac")
PREPROCESS_DIR = workflow_dir("图片预处理", "preprocessing")
MOVE_DIR = workflow_dir("移动文件", "moving")
CONFIG_PATH = AIO_DIR / "aio_config.json"


def load_archive_module() -> ModuleType:
    """Load the existing archive program as a library, without editing or starting its GUI."""
    script = MOVE_DIR / "tag_move_app.py"
    if not script.is_file():
        raise FileNotFoundError(f"找不到现有的安全归档程序：{script}")
    spec = importlib.util.spec_from_file_location("aio_existing_tag_move", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法载入现有的安全归档程序")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AioApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("图片分类 AIO 控制中心")
        self.geometry("930x650")
        self.minsize(760, 540)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.archive_module: ModuleType | None = None
        self.auto_busy = False
        self.last_move_at = time.monotonic()
        self.monitor_started_at = time.monotonic()
        self._build_ui()
        self._load_config()
        self.after(250, self._poll_events)
        self.after(1000, self._monitor_tick)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self, padding=14)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="图片分类 AIO 控制中心", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(shell, text="原有三个程序保持不变；这里统一启动、查看状态，并可自动调用既有的安全归档流程。", foreground="#555555").pack(anchor="w", pady=(3, 12))

        quick = ttk.LabelFrame(shell, text="三种工作", padding=12)
        quick.pack(fill="x")
        self._program_row(quick, 0, "1. 图片预处理", "独立整理 QQ 缓存图片，可预览、复制或移动。", self._launch_preprocess)
        self._program_row(quick, 1, "2. 发送到 Mac 识别", "将图片发送到局域网 Mac，生成 Tag 报告。", self._launch_mac)
        self._program_row(quick, 2, "3. 图片 Tag 安全归档", "手动审阅报告并安全移动；也可由下面的自动规则触发。", self._launch_move)

        auto = ttk.LabelFrame(shell, text="自动安全归档", padding=12)
        auto.pack(fill="x", pady=(12, 0))
        auto.columnconfigure(1, weight=1)
        self.report_var = tk.StringVar(value=str(MAC_DIR / "mac_tag_reports"))
        self.library_var = tk.StringVar(value="G:\\image")
        self.batch_var = tk.StringVar(value="")
        self.minutes_var = tk.StringVar(value="")
        self.auto_enabled = tk.BooleanVar(value=False)
        self.condition_var = tk.StringVar(value="自动归档未开启")
        self._path_row(auto, 0, "Mac Tag 报告目录", self.report_var, self._choose_report_root)
        self._path_row(auto, 1, "目标图片库", self.library_var, self._choose_library)
        settings = ttk.Frame(auto)
        settings.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Label(settings, text="已完成数量达到").pack(side="left")
        ttk.Entry(settings, textvariable=self.batch_var, width=8).pack(side="left", padx=(4, 4))
        ttk.Label(settings, text="张，或等待").pack(side="left")
        ttk.Entry(settings, textvariable=self.minutes_var, width=8).pack(side="left", padx=(4, 4))
        ttk.Label(settings, text="分钟，就自动安全移动。留空即不使用该条件。至少填一项。").pack(side="left")
        controls = ttk.Frame(auto)
        controls.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.auto_button = ttk.Button(controls, text="开启自动归档", command=self._toggle_auto)
        self.auto_button.pack(side="left")
        ttk.Button(controls, text="现在检查一次", command=self._manual_check).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="保存设置", command=self._save_config).pack(side="left", padx=(8, 0))
        ttk.Label(controls, textvariable=self.condition_var).pack(side="left", padx=(18, 0))

        state = ttk.LabelFrame(shell, text="运行记录", padding=8)
        state.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(state, height=10, wrap="word", state="disabled", background="#fbfbfb")
        scrollbar = ttk.Scrollbar(state, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._write_log("控制中心已就绪。自动归档默认关闭。")

    def _program_row(self, parent: ttk.LabelFrame, row: int, name: str, detail: str, command: object) -> None:
        ttk.Label(parent, text=name, width=22).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Label(parent, text=detail).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Button(parent, text="打开", command=command).grid(row=row, column=2, sticky="e", padx=(12, 0), pady=4)
        parent.columnconfigure(1, weight=1)

    def _path_row(self, parent: ttk.LabelFrame, row: int, label: str, variable: tk.StringVar, chooser: object) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择…", command=chooser).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)

    def _write_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _launch_existing(self, batch: Path, label: str) -> None:
        if not batch.is_file():
            messagebox.showerror("无法启动", f"找不到 {label} 的启动文件：\n{batch}")
            return
        try:
            subprocess.Popen(["cmd", "/c", "start", "", str(batch)], cwd=str(batch.parent))
            self._write_log(f"已打开：{label}。")
        except OSError as exc:
            messagebox.showerror("无法启动", str(exc))

    def _launch_preprocess(self) -> None:
        self._launch_existing(PREPROCESS_DIR / "启动预分类.bat", "图片预处理")

    def _launch_mac(self) -> None:
        self._launch_existing(MAC_DIR / "启动Mac图片Tag客户端.bat", "Mac 图片 Tag 客户端")

    def _launch_move(self) -> None:
        self._launch_existing(MOVE_DIR / "启动图片Tag安全归档.bat", "图片 Tag 安全归档")

    def _choose_report_root(self) -> None:
        value = filedialog.askdirectory(title="选择 Mac Tag 报告目录")
        if value:
            self.report_var.set(value)

    def _choose_library(self) -> None:
        value = filedialog.askdirectory(title="选择目标图片库")
        if value:
            self.library_var.set(value)

    @staticmethod
    def _positive(value: str, name: str, integer: bool = False) -> float | int | None:
        value = value.strip()
        if not value:
            return None
        try:
            result = int(value) if integer else float(value)
        except ValueError as exc:
            raise ValueError(f"{name}必须是正数") from exc
        if result <= 0:
            raise ValueError(f"{name}必须大于 0")
        return result

    def _auto_settings(self) -> tuple[Path, Path, int | None, float | None]:
        report_root = Path(self.report_var.get().strip())
        library_value = self.library_var.get().strip()
        library_root = Path(library_value)
        if not report_root.is_dir():
            raise ValueError("请选择有效的 Mac Tag 报告目录")
        if not library_value:
            raise ValueError("请填写目标图片库目录")
        batch = self._positive(self.batch_var.get(), "已完成数量", integer=True)
        minutes = self._positive(self.minutes_var.get(), "等待时间")
        if batch is None and minutes is None:
            raise ValueError("请至少填写“已完成数量”或“等待时间”其中一项")
        return report_root, library_root, batch, None if minutes is None else float(minutes) * 60

    def _toggle_auto(self) -> None:
        if self.auto_enabled.get():
            self.auto_enabled.set(False)
            self.auto_button.configure(text="开启自动归档")
            self.condition_var.set("自动归档已暂停")
            self._write_log("自动归档已暂停；不会再自动移动图片。")
            return
        try:
            self._auto_settings()
        except ValueError as exc:
            messagebox.showerror("无法开启", str(exc))
            return
        warning = (
            "开启后，当满足数量或等待时间条件时，控制中心会调用现有“图片 Tag 安全归档”的原有安全流程。\n\n"
            "流程仍是：完整复制 → SHA-256 校验 → 写入标签数据库 → 删除已验证原图。\n"
            "复制或校验有任何失败时，不会删除原图。\n\n是否开启？"
        )
        if not messagebox.askyesno("确认开启自动归档", warning, icon="warning"):
            return
        self.auto_enabled.set(True)
        self.auto_button.configure(text="暂停自动归档")
        self.last_move_at = time.monotonic()
        self.monitor_started_at = self.last_move_at
        self.condition_var.set("正在监控 Mac Tag 报告…")
        self._write_log("自动归档已开启。")
        self._save_config(silent=True)
        self._start_check("开启后首次检查")

    def _manual_check(self) -> None:
        if self.auto_busy:
            return
        try:
            self._auto_settings()
        except ValueError as exc:
            messagebox.showerror("无法检查", str(exc))
            return
        self._start_check("手动检查", force=False)

    def _monitor_tick(self) -> None:
        if self.auto_enabled.get() and not self.auto_busy:
            self._start_check("定时检查")
        self.after(1000, self._monitor_tick)

    def _start_check(self, reason: str, force: bool = False) -> None:
        if self.auto_busy:
            return
        try:
            report_root, library_root, batch, seconds = self._auto_settings()
        except ValueError as exc:
            if self.auto_enabled.get():
                self.auto_enabled.set(False)
                self.auto_button.configure(text="开启自动归档")
                self.condition_var.set(f"已停止：{exc}")
                self._write_log(f"自动归档已停止：{exc}")
            return
        self.auto_busy = True

        def worker() -> None:
            try:
                module = self.archive_module or load_archive_module()
                self.archive_module = module
                entries = module.load_latest_successful_tags(report_root)
                plans = module.make_plans(entries, library_root)
                workable = [plan for plan in plans if plan.state in {"ready", "recovery"}]
                elapsed = time.monotonic() - self.last_move_at
                due_batch = batch is not None and len(workable) >= batch
                due_time = seconds is not None and bool(workable) and elapsed >= seconds
                self.events.put(("checked", (reason, library_root, workable, due_batch, due_time, elapsed, seconds, force)))
            except Exception as exc:
                self.events.put(("check_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_auto_move(self, library_root: Path, plans: list[object]) -> None:
        self._write_log(f"条件已满足，开始自动安全归档 {len(plans)} 张图片…")
        self.condition_var.set(f"正在安全移动 {len(plans)} 张图片…")

        def worker() -> None:
            try:
                assert self.archive_module is not None
                result = self.archive_module.execute_safe_move(
                    plans, library_root, lambda kind, payload: self.events.put(("move_progress", (kind, payload)))
                )
                self.events.put(("move_finished", result))
            except Exception as exc:
                self.events.put(("move_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "checked":
                    reason, library_root, workable, due_batch, due_time, elapsed, seconds, force = payload
                    self.auto_busy = False
                    elapsed_minutes = elapsed / 60
                    if due_batch or due_time:
                        triggers = "、".join(filter(None, ["数量条件" if due_batch else "", "时间条件" if due_time else ""]))
                        self._write_log(f"{reason}：发现 {len(workable)} 张可归档图片，满足{triggers}。")
                        self.auto_busy = True
                        self._start_auto_move(library_root, workable)
                    else:
                        remaining = ""
                        if seconds is not None:
                            remaining = f"；距时间条件约 {max(0, seconds - elapsed) / 60:.1f} 分钟"
                        self.condition_var.set(f"可归档 {len(workable)} 张，已等待 {elapsed_minutes:.1f} 分钟{remaining}")
                        if reason == "手动检查":
                            self._write_log(f"手动检查：可归档 {len(workable)} 张，当前未触发自动移动。")
                elif kind == "move_progress":
                    phase, data = payload
                    index, total, plan = data
                    labels = {"copy": "复制并校验", "promote": "整理到分类目录", "delete": "删除已验证原图"}
                    self.condition_var.set(f"{labels.get(phase, phase)} {index}/{total}：{plan.entry.source.name}")
                elif kind == "move_finished":
                    self.auto_busy = False
                    self.last_move_at = time.monotonic()
                    self.monitor_started_at = self.last_move_at
                    result = payload
                    moved = result.get("moved", 0)
                    failed = result.get("delete_failed", [])
                    self.condition_var.set(f"自动归档完成：已移动 {moved} 张")
                    self._write_log(f"自动安全归档完成：已移动 {moved} 张；删除失败 {len(failed)} 张。")
                    if failed:
                        messagebox.showwarning("自动归档有待处理项目", f"已移动 {moved} 张，但有 {len(failed)} 张原图未能删除；安全副本和记录均已保留。")
                elif kind == "check_error":
                    self.auto_busy = False
                    self.condition_var.set(f"检查失败：{payload}")
                    self._write_log(f"自动归档检查失败：{payload}")
                elif kind == "move_error":
                    self.auto_busy = False
                    self.condition_var.set(f"自动归档未完成：{payload}")
                    self._write_log(f"自动归档未完成：{payload}")
                    messagebox.showerror("自动归档未完成", f"{payload}\n\n原图在复制/校验失败时不会被删除。")
        except queue.Empty:
            pass
        self.after(250, self._poll_events)

    def _config_data(self) -> dict[str, object]:
        return {
            "version": 1,
            "report_root": self.report_var.get().strip(),
            "library_root": self.library_var.get().strip(),
            "batch_size": self.batch_var.get().strip(),
            "minutes": self.minutes_var.get().strip(),
        }

    def _save_config(self, silent: bool = False) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self._config_data(), ensure_ascii=False, indent=2), encoding="utf-8")
            if not silent:
                self._write_log("设置已保存到 AIO 文件夹。")
        except OSError as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc))

    def _load_config(self) -> None:
        if not CONFIG_PATH.is_file():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.report_var.set(str(data.get("report_root", self.report_var.get())))
            self.library_var.set(str(data.get("library_root", self.library_var.get())))
            self.batch_var.set(str(data.get("batch_size", "")))
            self.minutes_var.set(str(data.get("minutes", "")))
            self._write_log("已读取上次保存的自动归档设置。")
        except (OSError, json.JSONDecodeError) as exc:
            self._write_log(f"未读取设置：{exc}")


if __name__ == "__main__":
    AioApp().mainloop()
