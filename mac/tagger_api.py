"""Private LAN image tagging API backed by an MLX vision-language model."""

import asyncio
import gc
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from pydantic import BaseModel
from starlette.formparsers import MultiPartParser

# Teach Pillow to decode iPhone/iPad HEIC and HEIF photos before uploads are
# opened in tag_image(). They are converted to JPEG in the existing pipeline.
register_heif_opener()

MODEL_PRESETS = {
    "4b": "mlx-community/Qwen3-VL-4B-Instruct-4bit",
    "8b": "mlx-community/Qwen3-VL-8B-Instruct-4bit",
}
DEFAULT_PRESET = os.environ.get("MODEL_PRESET", "4b").lower()
INITIAL_MODEL_ID = os.environ.get("MODEL_ID", MODEL_PRESETS.get(DEFAULT_PRESET, MODEL_PRESETS["4b"]))
API_KEY = os.environ.get("TAGGER_API_KEY", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_IMAGE_EDGE = int(os.environ.get("MAX_IMAGE_EDGE", "2048"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "350"))
# Images at or below this size remain in RAM after preprocessing and are handed
# straight to MLX-VLM as PIL images. Larger images use the former temporary
# JPEG path so an unusually large upload cannot pressure unified memory.
IN_MEMORY_IMAGE_MAX_PIXELS = int(os.environ.get("IN_MEMORY_IMAGE_MAX_PIXELS", "3000000"))
# Starlette otherwise spills uploads above 1 MB to a temporary file before our
# handler runs. Keep typical phone and screenshot uploads in memory too.
MULTIPART_MEMORY_MB = min(MAX_UPLOAD_MB, int(os.environ.get("MULTIPART_MEMORY_MB", "8")))
MultiPartParser.spool_max_size = MULTIPART_MEMORY_MB * 1024 * 1024

# MLX model inference is deliberately serialized.  On a 16 GB Mac this avoids
# duplicate, memory-heavy vision encodes when two Windows clients upload together.
inference_lock = asyncio.Lock()
runtime: dict[str, Any] = {}
WEB_PAGE = Path(__file__).with_name("web") / "index.html"
REMOTE_WEB_DIR = Path(os.environ.get("REMOTE_WEB_DIR", str(Path(__file__).resolve().parent.parent / "remote_web")))
REMOTE_WEB_PORT = int(os.environ.get("REMOTE_WEB_PORT", "8788"))
logger = logging.getLogger("image_tagger")

PROMPT = """Analyze this image for a private image library. Return JSON only, with no Markdown.
Schema:
{
  "category": one of ["anime_illustration", "chat_screenshot", "meme_or_sticker", "casual_photo", "portrait_photo", "professional_photography", "wallpaper_or_desktop", "document_or_ui", "other"],
  "tags": [5 to 10 concise, non-repeating Chinese tags],
  "character_candidates": [{"name": "official character name", "confidence": 0.0 to 1.0}],
  "contains_text": true or false,
  "summary": "one short Chinese sentence"
}
Rules: when a fictional character is plausibly recognizable from visual evidence, return up to 3 ranked candidates and use confidence to express uncertainty; return [] only when there is no reasonable visual basis. Never present a candidate as a fact.
Category policy:
- Use "wallpaper_or_desktop" only for a clean image genuinely suitable as a computer wallpaper: landscape composition approximately 16:9, with no desktop icons, Dock/taskbar, status bar, app window, notification, launcher, or other device UI. Do not use this category for square, portrait, or ordinary photo images merely because they could be used as a background.
- Any computer or phone home-screen / desktop screenshot showing icons, Dock/taskbar, status bar, windows, widgets, notifications, app UI, or chat UI must be "document_or_ui", never "wallpaper_or_desktop".
- Real-person sexy portraits, glamour photos, lingerie/swimwear photos, and real-person cosplay photos are "portrait_photo", even when studio-lit, professionally photographed, or highly stylized. In particular, a photo of a woman cosplaying an anime/game/fictional character is "portrait_photo" (including selfies, full-body shots, and outdoor or studio photos), not "anime_illustration" or "professional_photography"; classify by the real photographic subject, not the costume or character. Reserve "professional_photography" for non-portrait photographic works where the photographic subject/style is primary.
For anime, include style, hair, clothing, pose, setting and mood tags. For screenshots, identify app/UI/chat and visual layout, but do not transcribe private message content. For memes/stickers, tag visual subject and emotion. Do not infer real identities, nationality, religion, health, sexuality, or other sensitive personal traits."""


class TagResult(BaseModel):
    category: str
    tags: list[str]
    character_candidates: list[dict[str, Any]]
    contains_text: bool
    summary: str
    model: str


class ModelSwitchRequest(BaseModel):
    preset: str


class ModelResponseError(ValueError):
    """Keeps the model's text available when debug mode catches a parse failure."""

    def __init__(self, message: str, raw_response: str) -> None:
        super().__init__(message)
        self.raw_response = raw_response


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(503, "TAGGER_API_KEY has not been configured on the server")
    if x_api_key != API_KEY:
        raise HTTPException(401, "invalid X-API-Key")


def _load_model(model_id: str) -> None:
    from mlx_vlm import load
    from mlx_vlm.utils import load_config

    model, processor = load(model_id)
    runtime["model"] = model
    runtime["processor"] = processor
    runtime["config"] = load_config(model_id)
    runtime["model_id"] = model_id


def _unload_model() -> None:
    runtime.clear()
    gc.collect()
    # MLX keeps weights in unified memory, so clear its allocator before loading
    # a different model, especially when moving to 8B on a 16 GB Mac.
    import mlx.core as mx
    mx.clear_cache()


def _batch_console_online() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", REMOTE_WEB_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _batch_console_status() -> dict[str, Any]:
    process = runtime.get("batch_console_process")
    managed = bool(process and process.poll() is None)
    if process and not managed:
        runtime.pop("batch_console_process", None)
    return {
        "status": "running" if _batch_console_online() else "stopped",
        "port": REMOTE_WEB_PORT,
        "managed_by_tagger": managed,
    }


def _start_batch_console() -> dict[str, Any]:
    status = _batch_console_status()
    if status["status"] == "running":
        return status
    script = REMOTE_WEB_DIR / "start.sh"
    if not script.is_file():
        raise HTTPException(503, f"批量控制台启动脚本不存在：{script}")
    if not (REMOTE_WEB_DIR / ".venv" / "bin" / "python").is_file():
        raise HTTPException(503, "批量控制台尚未安装运行环境")
    log_path = Path(tempfile.gettempdir()) / f"remote-web-{REMOTE_WEB_PORT}.log"
    try:
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [str(script)],
            cwd=REMOTE_WEB_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        runtime["batch_console_process"] = process
        runtime["batch_console_log"] = log_handle
    except OSError as exc:
        raise HTTPException(502, f"无法启动批量控制台：{exc}") from exc
    for _ in range(30):
        if _batch_console_online():
            return _batch_console_status()
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise HTTPException(502, f"批量控制台未能启动，请查看日志：{log_path}")


def _stop_batch_console() -> dict[str, Any]:
    process = runtime.get("batch_console_process")
    if not process or process.poll() is not None:
        if _batch_console_online():
            raise HTTPException(409, "批量控制台不是由当前 8787 服务启动，无法安全停止")
        return _batch_console_status()
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    runtime.pop("batch_console_process", None)
    log_handle = runtime.pop("batch_console_log", None)
    if log_handle:
        log_handle.close()
    return _batch_console_status()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Loading once also downloads the model on its first ever launch.
    await asyncio.to_thread(_load_model, INITIAL_MODEL_ID)
    yield
    runtime.clear()


app = FastAPI(
    title="Private LAN Image Tagger",
    version="0.1.0",
    docs_url=None,  # Avoid advertising an unauthenticated interactive endpoint on the LAN.
    redoc_url=None,
    lifespan=lifespan,
)


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return JSON")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model result must be an object")
    return value


def _normalize(result: dict[str, Any]) -> TagResult:
    categories = {
        "anime_illustration", "chat_screenshot", "meme_or_sticker", "casual_photo",
        "portrait_photo", "professional_photography", "wallpaper_or_desktop",
        "document_or_ui", "other",
    }
    category = result.get("category", "other")
    if category not in categories:
        category = "other"
    tags = result.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
    # VLMs occasionally return equally usable variants such as
    # {"characters": ["Hatsune Miku"]}.  Preserve those candidates instead
    # of silently hiding them just because they do not exactly match the
    # requested object-array schema.
    candidates = result.get("character_candidates")
    if candidates is None:
        candidates = result.get("characters", result.get("character", []))
    if isinstance(candidates, (str, dict)):
        candidates = [candidates]
    if not isinstance(candidates, list):
        candidates = []
    cleaned_candidates = []
    for candidate in candidates[:5]:
        if isinstance(candidate, str):
            name, confidence_value = candidate.strip(), 0.5
        elif isinstance(candidate, dict):
            name = str(
                candidate.get("name")
                or candidate.get("character_name")
                or candidate.get("character")
                or ""
            ).strip()
            confidence_value = candidate.get(
                "confidence", candidate.get("score", candidate.get("probability", 0.5))
            )
        else:
            continue
        if not name:
            continue
        try:
            confidence_text = str(confidence_value).strip().removesuffix("%")
            confidence = float(confidence_text)
            if str(confidence_value).strip().endswith("%"):
                confidence /= 100
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5
        cleaned_candidates.append({"name": name, "confidence": confidence})
    return TagResult(
        category=category,
        tags=tags,
        character_candidates=cleaned_candidates,
        contains_text=bool(result.get("contains_text", False)),
        summary=str(result.get("summary", ""))[:300],
        model=runtime["model_id"],
    )


def _infer(image: Image.Image | str, capture_raw_response: bool = False) -> TagResult | tuple[TagResult, str]:
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    formatted_prompt = apply_chat_template(
        runtime["processor"], runtime["config"], PROMPT, num_images=1
    )
    result = generate(
        runtime["model"], runtime["processor"], formatted_prompt, [image],
        max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0, verbose=False,
    )
    # mlx-vlm >= 0.7 returns GenerationResult; older releases returned str.
    response = getattr(result, "text", result)
    raw_response = str(response)
    try:
        normalized = _normalize(_extract_json(raw_response))
    except ValueError as exc:
        if capture_raw_response:
            raise ModelResponseError(str(exc), raw_response) from exc
        raise
    return (normalized, raw_response) if capture_raw_response else normalized


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "model": runtime.get("model_id", INITIAL_MODEL_ID)}


@app.get("/", include_in_schema=False)
def test_page() -> FileResponse:
    return FileResponse(WEB_PAGE, media_type="text/html; charset=utf-8")


@app.get("/v1/models", dependencies=[Depends(_require_api_key)])
def list_models() -> dict[str, Any]:
    return {"current_model": runtime.get("model_id", INITIAL_MODEL_ID), "presets": MODEL_PRESETS}


@app.get("/v1/batch-console/status", dependencies=[Depends(_require_api_key)])
def batch_console_status() -> dict[str, Any]:
    return _batch_console_status()


@app.post("/v1/batch-console/start", dependencies=[Depends(_require_api_key)])
async def start_batch_console() -> dict[str, Any]:
    return await asyncio.to_thread(_start_batch_console)


@app.post("/v1/batch-console/stop", dependencies=[Depends(_require_api_key)])
async def stop_batch_console() -> dict[str, Any]:
    return await asyncio.to_thread(_stop_batch_console)


@app.post("/v1/model", dependencies=[Depends(_require_api_key)])
async def switch_model(request: ModelSwitchRequest) -> dict[str, str]:
    target = MODEL_PRESETS.get(request.preset.lower())
    if not target:
        raise HTTPException(400, "preset must be '4b' or '8b'")
    async with inference_lock:
        previous = runtime.get("model_id")
        if previous == target:
            return {"model": target, "status": "already_active"}
        await asyncio.to_thread(_unload_model)
        try:
            await asyncio.to_thread(_load_model, target)
        except Exception as exc:
            if previous:
                # A failed 8B load should not leave the LAN service unusable.
                await asyncio.to_thread(_load_model, previous)
            raise HTTPException(502, f"failed to load requested model: {exc}") from exc
    return {"model": target, "status": "active"}


@app.post("/v1/tag", response_model=None, dependencies=[Depends(_require_api_key)])
async def tag_image(
    image: UploadFile = File(...),
    x_debug_raw_model: str | None = Header(default=None),
) -> Any:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(415, "upload an image file in the 'image' form field")
    payload = await image.read()
    if not payload:
        raise HTTPException(400, "empty upload")
    if len(payload) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"image exceeds {MAX_UPLOAD_MB} MB")

    suffix = Path(image.filename or "image.jpg").suffix or ".jpg"
    temp_path = ""
    capture_raw_response = (x_debug_raw_model or "").strip().lower() in {"1", "true", "yes"}
    try:
        # Decode and normalize the image in RAM. MLX-VLM accepts PIL images,
        # avoiding an SSD write for ordinary uploads. The disk fallback keeps
        # pathological 2K+ images from raising unified-memory pressure.
        with Image.open(__import__("io").BytesIO(payload)) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            source.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            async with inference_lock:
                if source.width * source.height <= IN_MEMORY_IMAGE_MAX_PIXELS:
                    inference = await asyncio.to_thread(_infer, source, capture_raw_response)
                else:
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                        temp_path = temp.name
                    source.save(temp_path, format="JPEG", quality=92, optimize=True)
                    inference = await asyncio.to_thread(_infer, temp_path, capture_raw_response)
        if capture_raw_response:
            result, raw_model_response = inference
            value = json.loads(result.json())
            value["raw_model_response"] = raw_model_response
            return value
        return inference
    except UnidentifiedImageError as exc:
        raise HTTPException(415, "the upload is not a readable image") from exc
    except ModelResponseError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"model returned an invalid tag result: {exc}",
                "raw_model_response": exc.raw_response[:10000],
            },
        )
    except ValueError as exc:
        raise HTTPException(502, f"model returned an invalid tag result: {exc}") from exc
    except Exception as exc:
        # Keep the complete traceback in the Mac terminal while returning a
        # concise, actionable error to the private LAN client.
        logger.exception("Image tagging failed")
        raise HTTPException(502, f"model inference failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
