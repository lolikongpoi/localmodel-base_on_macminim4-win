"""Private LAN image tagging API backed by an MLX vision-language model."""

import asyncio
import gc
import json
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from pydantic import BaseModel

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
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_IMAGE_EDGE = int(os.environ.get("MAX_IMAGE_EDGE", "2048"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "350"))

# MLX model inference is deliberately serialized.  On a 16 GB Mac this avoids
# duplicate, memory-heavy vision encodes when two Windows clients upload together.
inference_lock = asyncio.Lock()
runtime: dict[str, Any] = {}
WEB_PAGE = Path(__file__).with_name("web") / "index.html"
logger = logging.getLogger("image_tagger")

PROMPT = """Analyze this image for a private image library. Return JSON only, with no Markdown.
Schema:
{
  "category": one of ["anime_illustration", "chat_screenshot", "meme_or_sticker", "casual_photo", "portrait_photo", "professional_photography", "wallpaper_or_desktop", "document_or_ui", "other"],
  "tags": [5 to 20 concise Chinese tags],
  "character_candidates": [{"name": "official character name", "confidence": 0.0 to 1.0}],
  "contains_text": true or false,
  "summary": "one short Chinese sentence"
}
Rules: when a fictional character is plausibly recognizable from visual evidence, return up to 3 ranked candidates and use confidence to express uncertainty; return [] only when there is no reasonable visual basis. Never present a candidate as a fact.
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
    tags = [str(tag).strip() for tag in tags if str(tag).strip()][:20]
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


def _infer(image_path: str) -> TagResult:
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    formatted_prompt = apply_chat_template(
        runtime["processor"], runtime["config"], PROMPT, num_images=1
    )
    result = generate(
        runtime["model"], runtime["processor"], formatted_prompt, [image_path],
        max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0, verbose=False,
    )
    # mlx-vlm >= 0.7 returns GenerationResult; older releases returned str.
    response = getattr(result, "text", result)
    return _normalize(_extract_json(response))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "model": runtime.get("model_id", INITIAL_MODEL_ID)}


@app.get("/", include_in_schema=False)
def test_page() -> FileResponse:
    return FileResponse(WEB_PAGE, media_type="text/html; charset=utf-8")


@app.get("/v1/models", dependencies=[Depends(_require_api_key)])
def list_models() -> dict[str, Any]:
    return {"current_model": runtime.get("model_id", INITIAL_MODEL_ID), "presets": MODEL_PRESETS}


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


@app.post("/v1/tag", response_model=TagResult, dependencies=[Depends(_require_api_key)])
async def tag_image(image: UploadFile = File(...)) -> TagResult:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(415, "upload an image file in the 'image' form field")
    payload = await image.read()
    if not payload:
        raise HTTPException(400, "empty upload")
    if len(payload) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"image exceeds {MAX_UPLOAD_MB} MB")

    suffix = Path(image.filename or "image.jpg").suffix or ".jpg"
    temp_path = ""
    try:
        # Decode and re-save to validate the image, apply EXIF orientation, remove
        # metadata, and cap pathological huge screenshots before model inference.
        with Image.open(__import__("io").BytesIO(payload)) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            source.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                temp_path = temp.name
            source.save(temp_path, format="JPEG", quality=92, optimize=True)
        async with inference_lock:
            return await asyncio.to_thread(_infer, temp_path)
    except UnidentifiedImageError as exc:
        raise HTTPException(415, "the upload is not a readable image") from exc
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
