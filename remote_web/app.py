"""Remote controller for the private Mac image-tagging service.

The manually entered folder path is read on this web server's host. Browser
clients only control runs and download reports. Keys remain in memory only.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
WEB_PAGE = APP_DIR / "web" / "index.html"
REPORT_ROOT = Path(os.environ.get("REMOTE_WEB_REPORT_DIR", str(APP_DIR / "reports"))).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("REMOTE_WEB_MAX_UPLOAD_MB", "100")) * 1024 * 1024
ACCESS_TOKEN = os.environ.get("REMOTE_WEB_ACCESS_TOKEN", "")
ALLOWED_HOSTS = {v.strip().lower() for v in os.environ.get("TAGGER_ALLOWED_HOSTS", "").split(",") if v.strip()}
SOURCE_ROOTS = tuple(Path(v.strip()).expanduser().resolve() for v in os.environ.get("IMAGE_SOURCE_ROOTS", "").split(",") if v.strip())
# Optional root allow-list for manually entered report output folders.
REPORT_OUTPUT_ROOTS = tuple(Path(v.strip()).expanduser().resolve() for v in os.environ.get("REPORT_OUTPUT_ROOTS", "").split(",") if v.strip())
EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
FIELDS = ["source", "status", "elapsed_seconds", "category", "tags", "auto_character", "character_candidates", "contains_text", "summary", "model", "error", "details"]


class ServiceRequest(BaseModel):
    service_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(min_length=1, max_length=2048)
    preset: Optional[str] = None


class RunRequest(ServiceRequest):
    folder: str = Field(min_length=1, max_length=2000)
    output_folder: Optional[str] = Field(default=None, max_length=2000)
    windows_folder: Optional[str] = Field(default=None, max_length=2000)
    use_windows_path: bool = False
    debug_model_response: bool = False
    recursive: bool = True
    max_images: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    max_minutes: Optional[float] = Field(default=None, gt=0, le=10080)
    skip_completed: bool = True


class FolderCheckRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=2000)
    output_folder: Optional[str] = Field(default=None, max_length=2000)
    windows_folder: Optional[str] = Field(default=None, max_length=2000)
    use_windows_path: bool = False
    recursive: bool = True
    check_completed: bool = False


class RestartConsoleRequest(BaseModel):
    force: bool = False


@dataclass
class Record:
    source: str
    status: str
    elapsed_seconds: float
    category: str = ""
    tags: list[str] = field(default_factory=list)
    character_candidates: list[dict[str, Any]] = field(default_factory=list)
    auto_character: str = ""
    contains_text: Optional[bool] = None
    summary: str = ""
    model: str = ""
    error: str = ""
    details: str = ""
    raw_model_response: str = ""


@dataclass
class Run:
    id: str
    service_url: str
    api_key: str
    folder: Path
    windows_folder: Optional[str]
    use_windows_path: bool
    recursive: bool
    max_images: Optional[int]
    max_minutes: Optional[float]
    skip_completed: bool
    debug_model_response: bool
    directory: Path
    records: list[Record] = field(default_factory=list)
    planned_total: int = 0
    skipped: int = 0
    current_source: str = ""
    state: str = "preparing"
    stop_reason: str = ""
    fatal_error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    paused_at: Optional[float] = None
    paused_seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.resume.set()


runs: dict[str, Run] = {}
runs_lock = threading.Lock()


def require_access(x_remote_access_token: Optional[str] = Header(default=None)) -> None:
    if ACCESS_TOKEN and x_remote_access_token != ACCESS_TOKEN:
        raise HTTPException(401, "远程访问口令无效")


def service_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Mac 服务地址必须是完整的 http:// 或 https:// 地址")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HTTPException(422, "Mac 服务地址不要包含路径、参数或片段")
    if ALLOWED_HOSTS and parsed.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(403, "该 Mac 主机不在 TAGGER_ALLOWED_HOSTS 允许列表中")
    return value


def local_path(value: str, field_name: str) -> Path:
    """Turn a mounted macOS SMB URL into its local /Volumes path when possible."""
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "smb":
        return Path(raw).expanduser()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parsed.hostname or not parts:
        raise HTTPException(422, f"{field_name} SMB 地址格式无效")
    share, remainder = parts[0], parts[1:]
    volume_root = Path("/Volumes")
    candidates = [volume_root / share]
    try:
        candidates.extend(
            item for item in volume_root.iterdir()
            if item.is_dir() and (item.name == share or item.name.startswith(share + "-"))
        )
    except OSError:
        pass
    for candidate in candidates:
        path = candidate.joinpath(*remainder)
        if path.exists():
            return path
    expected = volume_root / share
    raise HTTPException(
        422,
        f"SMB 共享尚未挂载到这台 Mac。请先在访达连接该共享，或输入本机路径：{expected}",
    )


def folder_path(value: str) -> Path:
    try:
        folder = local_path(value, "图片文件夹路径").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(422, "该图片文件夹不存在，或 Web 服务没有访问权限") from exc
    if not folder.is_dir():
        raise HTTPException(422, "请输入图片文件夹路径，而不是单个文件路径")
    if SOURCE_ROOTS and not any(folder == root or root in folder.parents for root in SOURCE_ROOTS):
        raise HTTPException(403, "该路径不在 IMAGE_SOURCE_ROOTS 允许范围内")
    return folder


def output_folder_path(value: Optional[str]) -> Path:
    """Resolve a report root, creating it only after an explicit user request."""
    if not value or not value.strip():
        return REPORT_ROOT
    try:
        folder = local_path(value, "结果保存路径").resolve()
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(422, "无法创建结果保存路径，或 Web 服务没有写入权限") from exc
    if not folder.is_dir():
        raise HTTPException(422, "结果保存路径必须是文件夹路径")
    if REPORT_OUTPUT_ROOTS and not any(folder == root or root in folder.parents for root in REPORT_OUTPUT_ROOTS):
        raise HTTPException(403, "该路径不在 REPORT_OUTPUT_ROOTS 允许范围内")
    return folder


def windows_folder_path(value: Optional[str], enabled: bool) -> Optional[str]:
    if not enabled:
        return None
    folder = (value or "").strip().replace("/", "\\").rstrip("\\")
    if not folder:
        raise HTTPException(422, "请填写图片文件夹对应的 Windows 本地路径")
    if not (re.match(r"^[a-zA-Z]:\\", folder) or folder.startswith("\\\\")):
        raise HTTPException(422, "Windows 路径应以盘符（如 D:\\）或网络路径（如 \\\\服务器\\共享）开头")
    return folder


def mac_request(url: str, key: str, method: str, endpoint: str, body: Optional[bytes] = None, content_type: Optional[str] = None, timeout: int = 30, needs_key: bool = True, extra_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if needs_key:
        headers["X-API-Key"] = key
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    try:
        with urlopen(Request(url + endpoint, data=body, headers=headers, method=method), timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raw_model_response = ""
        try:
            value = json.loads(raw)
            detail = value.get("detail", f"Mac 服务返回 HTTP {exc.code}")
            if isinstance(value, dict):
                raw_model_response = str(value.get("raw_model_response", ""))[:10000]
        except (json.JSONDecodeError, AttributeError):
            detail = f"Mac 服务返回 HTTP {exc.code}"
        error = HTTPException(exc.code, str(detail))
        error.raw_model_response = raw_model_response
        raise error from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "无法连接 Mac 服务。请检查地址、VPN/局域网、防火墙和服务状态。") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Mac 服务返回了非 JSON 响应") from exc
    if not isinstance(value, dict):
        raise HTTPException(502, "Mac 服务返回格式异常")
    return value


def images(folder: Path, recursive: bool) -> list[Path]:
    iterator: Iterable[Path] = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in iterator if p.is_file() and p.suffix.lower() in EXTENSIONS), key=lambda p: str(p).lower())


def normalized(path: str | Path) -> str:
    value = str(path).strip()
    if re.match(r"^[a-zA-Z]:[\\/]", value) or value.startswith("\\\\"):
        return value.replace("/", "\\").rstrip("\\").casefold()
    return os.path.normcase(os.path.abspath(os.path.normpath(value))).casefold()


def report_source(folder: Path, windows_folder: Optional[str], use_windows_path: bool, path: Path) -> str:
    if not use_windows_path:
        return str(path)
    assert windows_folder
    return windows_folder + "\\" + "\\".join(path.relative_to(folder).parts)


def previous_successes(report_root: Path) -> set[str]:
    result: set[str] = set()
    for path in report_root.glob("*/image_tags.csv") if report_root.is_dir() else []:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if (row.get("status") or "").lower() == "success" and row.get("source"):
                        result.add(normalized(row["source"]))
        except (OSError, UnicodeError, csv.Error):
            continue
    return result


def record_from_response(source: str, response: dict[str, Any], elapsed: float) -> Record:
    raw_candidates = response.get("character_candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    cleaned: list[dict[str, Any]] = []
    auto = ""
    for item in candidates[:5]:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        name = str(item["name"]).strip()
        cleaned.append({"name": name, "confidence": confidence})
        if confidence >= .8 and not auto:
            auto = name
    tags = response.get("tags")
    return Record(source, "success", round(elapsed, 3), str(response.get("category", "")), [str(x).strip() for x in tags if str(x).strip()] if isinstance(tags, list) else [], cleaned, auto, response.get("contains_text") if isinstance(response.get("contains_text"), bool) else None, str(response.get("summary", ""))[:500], str(response.get("model", "")), raw_model_response=str(response.get("raw_model_response", ""))[:10000])


def append_record(run: Run, record: Record) -> None:
    serialized = asdict(record)
    serialized.pop("raw_model_response", None)
    jsonl = run.directory / "image_tags.jsonl"
    with jsonl.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(serialized, ensure_ascii=False) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    csv_file = run.directory / "image_tags.csv"
    new = not csv_file.exists()
    with csv_file.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new: writer.writeheader()
        row = serialized
        row["tags"] = " | ".join(record.tags)
        row["character_candidates"] = json.dumps(record.character_candidates, ensure_ascii=False)
        writer.writerow(row)
        handle.flush(); os.fsync(handle.fileno())
    run.records.append(record)


def tag_file(run: Run, path: Path) -> Record:
    started = time.monotonic()
    source = report_source(run.folder, run.windows_folder, run.use_windows_path, path)
    try:
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"图片超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制")
        boundary = f"----RemoteMacTag{uuid.uuid4().hex}"
        name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body = prefix + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        for attempt in range(2):
            try:
                response = mac_request(run.service_url, run.api_key, "POST", "/v1/tag", body, f"multipart/form-data; boundary={boundary}", 180, extra_headers={"X-Debug-Raw-Model": "1"} if run.debug_model_response else None)
                return record_from_response(source, response, time.monotonic() - started)
            except HTTPException as exc:
                if attempt == 0 and exc.status_code == 502:
                    time.sleep(1.5); continue
                raise
        raise AssertionError("unreachable")
    except HTTPException as exc:
        return Record(source, "error", round(time.monotonic() - started, 3), error=str(exc.detail), details=f"HTTP {exc.status_code}", raw_model_response=str(getattr(exc, "raw_model_response", ""))[:10000])
    except Exception as exc:
        return Record(source, "error", round(time.monotonic() - started, 3), error="读取或发送图片时出错", details=f"{type(exc).__name__}: {exc}"[:1000])


def active_elapsed(run: Run, now: Optional[float] = None) -> float:
    if run.started_at is None:
        return 0.0
    current = time.monotonic() if now is None else now
    active_pause = current - run.paused_at if run.paused_at is not None else 0.0
    return max(0.0, current - run.started_at - run.paused_seconds - active_pause)


def finish(run: Run, reason: str) -> None:
    with run.lock:
        if run.state in {"finished", "failed"}: return
        if run.paused_at is not None:
            run.paused_seconds += time.monotonic() - run.paused_at
            run.paused_at = None
        run.state = "failed" if reason == "error" else "finished"
        run.stop_reason, run.current_source = reason, ""
        summary = {"source_folder": str(run.folder), "windows_folder": run.windows_folder, "use_windows_path": run.use_windows_path, "service_url": run.service_url, "planned_total": run.planned_total, "skipped_completed": run.skipped, "processed": len(run.records), "successful": sum(x.status == "success" for x in run.records), "failed": sum(x.status != "success" for x in run.records), "stop_reason": reason, "total_elapsed_seconds": round(active_elapsed(run), 3), "csv": "image_tags.csv", "jsonl": "image_tags.jsonl"}
        (run.directory / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def worker(run: Run) -> None:
    try:
        if mac_request(run.service_url, run.api_key, "GET", "/healthz", timeout=15, needs_key=False).get("status") != "ok":
            raise RuntimeError("Mac 服务健康检查未通过")
        paths = images(run.folder, run.recursive)
        if run.skip_completed:
            done = previous_successes(run.directory.parent)
            kept = [p for p in paths if normalized(report_source(run.folder, run.windows_folder, run.use_windows_path, p)) not in done]
            run.skipped, paths = len(paths) - len(kept), kept
        if run.max_images is not None: paths = paths[:run.max_images]
        with run.lock:
            run.planned_total, run.state = len(paths), "running"
            run.started_at = time.monotonic()
        deadline = None if run.max_minutes is None else time.monotonic() + run.max_minutes * 60
        for path in paths:
            if run.stop.is_set(): finish(run, "user_stop"); return
            while not run.resume.is_set():
                with run.lock:
                    run.state = "paused"
                    if run.paused_at is None:
                        run.paused_at = time.monotonic()
                if run.stop.wait(.25): finish(run, "user_stop"); return
            if run.paused_at is not None:
                with run.lock:
                    pause_duration = time.monotonic() - run.paused_at
                    run.paused_seconds += pause_duration
                    run.paused_at = None
                if deadline is not None: deadline += pause_duration
            if deadline is not None and time.monotonic() >= deadline: finish(run, "time_limit"); return
            with run.lock:
                run.state, run.current_source = "running", report_source(run.folder, run.windows_folder, run.use_windows_path, path)
            item = tag_file(run, path)
            with run.lock: append_record(run, item)
        finish(run, "completed")
    except Exception as exc:
        with run.lock: run.fatal_error = f"{type(exc).__name__}: {exc}"
        finish(run, "error")


def get_run(run_id: str) -> Run:
    with runs_lock: run = runs.get(run_id)
    if not run: raise HTTPException(404, "找不到该任务。服务器重启后请新建任务。")
    return run


def active_runs() -> list[Run]:
    with runs_lock:
        return [run for run in runs.values() if run.state in {"preparing", "running", "paused"}]


def restart_console_process() -> None:
    """Replace this process after the HTTP response has been sent."""
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            os.environ.get("REMOTE_WEB_HOST", "0.0.0.0"),
            "--port",
            os.environ.get("REMOTE_WEB_PORT", "8788"),
        ],
    )


def run_data(run: Run, details: bool = False) -> dict[str, Any]:
    with run.lock:
        processed = len(run.records)
        remaining = max(0, run.planned_total - processed)
        estimated = None if not processed else round(sum(item.elapsed_seconds for item in run.records) / processed * remaining, 3)
        data: dict[str, Any] = {"id": run.id, "folder": str(run.folder), "report_folder": str(run.directory), "windows_folder": run.windows_folder, "use_windows_path": run.use_windows_path, "debug_model_response": run.debug_model_response, "planned_total": run.planned_total, "skipped": run.skipped, "processed": processed, "successful": sum(x.status == "success" for x in run.records), "failed": sum(x.status != "success" for x in run.records), "elapsed_seconds": round(active_elapsed(run), 3), "estimated_remaining_seconds": estimated, "state": run.state, "current_source": run.current_source, "stop_reason": run.stop_reason, "fatal_error": run.fatal_error, "created_at": datetime.fromtimestamp(run.created_at, timezone.utc).isoformat()}
        if details: data["records"] = [asdict(x) for x in run.records[-200:]]
        return data


app = FastAPI(title="Remote Mac Image Tagger", docs_url=None, redoc_url=None)


@app.get("/", include_in_schema=False)
def page() -> FileResponse: return FileResponse(WEB_PAGE, media_type="text/html; charset=utf-8")


@app.get("/api/config")
def config(_: None = Depends(require_access)) -> dict[str, Any]: return {"access_token_required": bool(ACCESS_TOKEN)}


@app.post("/api/folder/inspect")
def inspect_folder(payload: FolderCheckRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    folder = folder_path(payload.folder)
    windows_folder = windows_folder_path(payload.windows_folder, payload.use_windows_path)
    paths = images(folder, payload.recursive)
    result: dict[str, Any] = {
        "folder": str(folder),
        "recursive": payload.recursive,
        "image_count": len(paths),
        "use_windows_path": payload.use_windows_path,
    }
    if payload.check_completed:
        output_folder = output_folder_path(payload.output_folder)
        completed = previous_successes(output_folder)
        completed_count = sum(
            normalized(report_source(folder, windows_folder, payload.use_windows_path, path)) in completed
            for path in paths
        )
        result.update({
            "output_folder": str(output_folder),
            "completed_count": completed_count,
            "pending_count": len(paths) - completed_count,
        })
    return result


@app.post("/api/service/health")
def health(payload: ServiceRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    return mac_request(service_url(payload.service_url), payload.api_key.strip(), "GET", "/healthz", timeout=15, needs_key=False)


@app.post("/api/service/models")
def models(payload: ServiceRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    return mac_request(service_url(payload.service_url), payload.api_key.strip(), "GET", "/v1/models")


@app.post("/api/service/model")
def model(payload: ServiceRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    preset = (payload.preset or "").strip()
    if not preset:
        raise HTTPException(422, "请先从后端模型列表选择模型")
    # The tagger owns the preset allow-list, including future model families.
    return mac_request(service_url(payload.service_url), payload.api_key.strip(), "POST", "/v1/model", json.dumps({"preset": preset}).encode(), "application/json", 600)


@app.post("/api/console/restart")
def restart_console(payload: RestartConsoleRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    active = active_runs()
    if active and not payload.force:
        raise HTTPException(409, "有批处理任务正在运行；请确认后使用强制重启")
    for run in active:
        run.stop.set()
        run.resume.set()
    # Give FastAPI time to flush this response before replacing the process.
    threading.Timer(0.7, restart_console_process).start()
    return {
        "status": "restarting",
        "interrupted_runs": len(active),
        "message": "服务将在约一秒内重新启动",
    }


@app.post("/api/runs", status_code=201)
def create(payload: RunRequest, _: None = Depends(require_access)) -> dict[str, Any]:
    target_url = service_url(payload.service_url)
    target_folder = folder_path(payload.folder)
    target_output_folder = output_folder_path(payload.output_folder)
    target_windows_folder = windows_folder_path(payload.windows_folder, payload.use_windows_path)
    run_id = uuid.uuid4().hex
    directory = target_output_folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
    run = Run(run_id, target_url, payload.api_key.strip(), target_folder, target_windows_folder, payload.use_windows_path, payload.recursive, payload.max_images, payload.max_minutes, payload.skip_completed, payload.debug_model_response, directory)
    with runs_lock:
        if any(item.state in {"preparing", "running", "paused"} for item in runs.values()):
            raise HTTPException(409, "已有批处理任务正在运行，请先暂停、停止或等待它完成")
        directory.mkdir(parents=True, exist_ok=False)
        runs[run_id] = run
    threading.Thread(target=worker, args=(run,), daemon=True, name=f"tag-run-{run_id[:8]}").start()
    return run_data(run)


@app.get("/api/runs/{run_id}")
def status(run_id: str, _: None = Depends(require_access)) -> dict[str, Any]: return run_data(get_run(run_id), True)




@app.post("/api/runs/{run_id}/{action}")
def control(run_id: str, action: str, _: None = Depends(require_access)) -> dict[str, Any]:
    run = get_run(run_id)
    if action == "pause": run.resume.clear()
    elif action == "resume": run.resume.set()
    elif action == "stop": run.stop.set(); run.resume.set()
    else: raise HTTPException(404, "未知操作")
    return run_data(run)


@app.get("/api/runs/{run_id}/report/{kind}")
def report(run_id: str, kind: str, _: None = Depends(require_access)) -> FileResponse:
    names = {"csv": "image_tags.csv", "jsonl": "image_tags.jsonl", "summary": "run_summary.json"}
    name = names.get(kind)
    if not name: raise HTTPException(404, "未知报告类型")
    path = get_run(run_id).directory / name
    if not path.exists(): raise HTTPException(404, "该报告尚未生成")
    return FileResponse(path, filename=name)
