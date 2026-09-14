from __future__ import annotations

import csv
import json
import mimetypes
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class TagServiceError(RuntimeError):
    def __init__(self, status: int | None, message: str, details: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def friendly_error(status: int | None, details: str = "") -> str:
    messages = {
        400: "请求无效：请确认图片文件正常。",
        401: "API key 不正确或已失效，请重新配置。",
        413: "图片超过服务端 20 MB 限制，请压缩或缩小后再试。",
        415: "服务无法读取此图片格式，请换用 JPG、PNG 等常见格式。",
        502: "Mac 本地模型推理或输出格式异常，已自动重试一次。",
    }
    if status in messages:
        return messages[status]
    if status is None:
        return "无法连接 Mac 服务：请检查地址、局域网、防火墙和服务状态。"
    return f"服务返回 HTTP {status}。"


def scan_images(folder: Path, recursive: bool = True) -> list[Path]:
    iterator: Iterable[Path] = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (item for item in iterator if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda item: str(item).lower(),
    )


def normalize_source_path(path: str | Path) -> str:
    """生成可跨报告比对的 Windows 路径键。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def find_completed_tag_sources(report_root: Path) -> set[str]:
    """读取已有报告中的成功项；不完整运行目录也会被识别，避免重复提交。"""
    if not report_root.is_dir():
        return set()
    completed: set[str] = set()
    for csv_path in report_root.glob("*/image_tags.csv"):
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    source = (row.get("source") or "").strip()
                    if source and (row.get("status") or "").strip().lower() == "success":
                        completed.add(normalize_source_path(source))
        except (OSError, csv.Error, UnicodeError):
            # 某一份损坏或仍在写入的旧报告不应阻止新的批处理任务。
            continue
    return completed


class MacTagService:
    def __init__(self, base_url: str, api_key: str):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Mac 地址必须以 http:// 或 https:// 开头，例如 http://192.168.1.23:8787")
        if not api_key.strip():
            raise ValueError("请输入 API key")
        self.base_url = base_url
        self.api_key = api_key.strip()

    def _request(
        self,
        method: str,
        endpoint: str,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: int = 30,
        with_key: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if with_key:
            headers["X-API-Key"] = self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(f"{self.base_url}{endpoint}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise TagServiceError(exc.code, friendly_error(exc.code, raw), raw) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TagServiceError(None, friendly_error(None), str(exc)) from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TagServiceError(502, friendly_error(502), raw[:1000]) from exc
        if not isinstance(value, dict):
            raise TagServiceError(502, friendly_error(502), raw[:1000])
        return value

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz", timeout=15, with_key=False)

    def models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models", timeout=30)

    def switch_model(self, preset: str) -> dict[str, Any]:
        if preset not in {"4b", "8b"}:
            raise ValueError("模型预设只能是 4b 或 8b")
        body = json.dumps({"preset": preset}, ensure_ascii=False).encode("utf-8")
        return self._request("POST", "/v1/model", body, "application/json", timeout=600)

    def tag(self, image_path: Path) -> dict[str, Any]:
        size = image_path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise TagServiceError(413, friendly_error(413), f"文件大小：{size} bytes")
        boundary = f"----LocalTagger{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        safe_name = image_path.name.replace('"', "_")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = prefix + image_path.read_bytes() + suffix
        return self._request("POST", "/v1/tag", body, f"multipart/form-data; boundary={boundary}", timeout=180)


@dataclass
class TagRecord:
    source: str
    status: str
    elapsed_seconds: float
    category: str = ""
    tags: list[str] | None = None
    character_candidates: list[dict[str, Any]] | None = None
    auto_character: str = ""
    contains_text: bool | None = None
    summary: str = ""
    model: str = ""
    error: str = ""
    details: str = ""

    def __post_init__(self) -> None:
        self.tags = self.tags or []
        self.character_candidates = self.character_candidates or []


REPORT_FIELDS = [
    "source", "status", "elapsed_seconds", "category", "tags", "auto_character", "character_candidates",
    "contains_text", "summary", "model", "error", "details",
]


class IncrementalReportWriter:
    """每张图片完成后立即落盘，避免长任务中断时丢失已完成结果。"""

    def __init__(self, output_root: Path, source_folder: Path, service_url: str):
        self.run_dir = output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.source_folder = source_folder
        self.service_url = service_url
        self.jsonl_path = self.run_dir / "image_tags.jsonl"
        self.csv_path = self.run_dir / "image_tags.csv"
        self.summary_path = self.run_dir / "run_summary.json"
        self.jsonl_handle = self.jsonl_path.open("w", encoding="utf-8")
        self.csv_handle = self.csv_path.open("w", encoding="utf-8-sig", newline="")
        self.csv_writer = csv.DictWriter(self.csv_handle, fieldnames=REPORT_FIELDS)
        self.csv_writer.writeheader()
        self._flush()
        self.count = 0
        self.successful = 0
        self.total_elapsed = 0.0
        self.closed = False

    def _flush(self) -> None:
        self.jsonl_handle.flush()
        self.csv_handle.flush()
        # 每张图片处理耗时通常远大于一次同步写盘；显式同步可防止异常退出丢失最后一批记录。
        os.fsync(self.jsonl_handle.fileno())
        os.fsync(self.csv_handle.fileno())

    def append(self, record: TagRecord) -> None:
        if self.closed:
            raise RuntimeError("报告已关闭")
        self.jsonl_handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        row = asdict(record)
        row["tags"] = " | ".join(record.tags or [])
        row["character_candidates"] = json.dumps(record.character_candidates, ensure_ascii=False)
        self.csv_writer.writerow(row)
        self._flush()
        self.count += 1
        self.successful += int(record.status == "success")
        self.total_elapsed += record.elapsed_seconds

    def finish(self, planned_total: int, stop_reason: str) -> Path:
        if self.closed:
            return self.run_dir
        self._flush()
        self.jsonl_handle.close()
        self.csv_handle.close()
        summary = {
            "source_folder": str(self.source_folder),
            "service_url": self.service_url,
            "planned_total": planned_total,
            "processed": self.count,
            "successful": self.successful,
            "failed": self.count - self.successful,
            "stop_reason": stop_reason,
            "total_elapsed_seconds": round(self.total_elapsed, 3),
            "csv": str(self.csv_path),
            "jsonl": str(self.jsonl_path),
        }
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.closed = True
        return self.run_dir


def record_from_response(path: Path, response: dict[str, Any], elapsed: float) -> TagRecord:
    candidates = response.get("character_candidates")
    if not isinstance(candidates, list):
        candidates = []
    normalized_candidates: list[dict[str, Any]] = []
    auto_character = ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = str(candidate.get("name", "")).strip()
        try:
            confidence = float(candidate.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized_candidates.append({"name": name, "confidence": confidence})
        if confidence >= 0.80 and not auto_character:
            auto_character = name
    tags = response.get("tags")
    return TagRecord(
        source=str(path),
        status="success",
        elapsed_seconds=elapsed,
        category=str(response.get("category", "")),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        character_candidates=normalized_candidates,
        auto_character=auto_character,
        contains_text=response.get("contains_text") if isinstance(response.get("contains_text"), bool) else None,
        summary=str(response.get("summary", "")),
        model=str(response.get("model", "")),
    )


def tag_with_single_retry(service: MacTagService, path: Path) -> TagRecord:
    started = time.monotonic()
    for attempt in range(2):
        try:
            response = service.tag(path)
            return record_from_response(path, response, time.monotonic() - started)
        except TagServiceError as exc:
            can_retry = attempt == 0 and (exc.status == 502 or exc.status is None)
            if can_retry:
                time.sleep(1.5)
                continue
            return TagRecord(
                source=str(path),
                status="error",
                elapsed_seconds=time.monotonic() - started,
                error=exc.message,
                details=exc.details[:1000],
            )
        except Exception as exc:
            return TagRecord(
                source=str(path),
                status="error",
                elapsed_seconds=time.monotonic() - started,
                error="客户端处理图片时出错。",
                details=f"{type(exc).__name__}: {exc}"[:1000],
            )
    raise AssertionError("unreachable")


def write_reports(records: list[TagRecord], output_root: Path, source_folder: Path, service_url: str) -> Path:
    writer = IncrementalReportWriter(output_root, source_folder, service_url)
    for record in records:
        writer.append(record)
    return writer.finish(planned_total=len(records), stop_reason="completed")
