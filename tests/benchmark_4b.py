"""Offline comparison on the same eight deterministic, synthetic images."""
import argparse
import gc
import json
import os
import sys
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "mac" if (ROOT / "mac").is_dir() else ROOT
sys.path.insert(0, str(MAC))
os.environ["HF_HUB_OFFLINE"] = "1"
import tagger_api as tagger


def cached_model(preset):
    cache = Path(os.environ.get("HF_HUB_CACHE", MAC / "models/huggingface/hub"))
    repo = cache / ("models--" + tagger.MODEL_PRESETS[preset].replace("/", "--"))
    snapshot = repo / "snapshots" / (repo / "refs/main").read_text().strip()
    if (snapshot / "model.safetensors").is_file():
        return snapshot
    index = snapshot / "model.safetensors.index.json"
    weights = set(json.loads(index.read_text())["weight_map"].values()) if index.is_file() else {"model.safetensors"}
    if not all((snapshot / name).is_file() for name in weights):
        raise FileNotFoundError(f"{preset}: weights not fully downloaded")
    return snapshot


def samples():
    sizes = [(640, 480), (640, 480), (768, 432), (480, 640)]
    colors = ["#214d79", "#826643", "#498451", "#bc6847", "#664d8b", "#455964", "#816f34", "#3d675f"]
    for i, color in enumerate(colors):
        w, h = sizes[i % len(sizes)]
        image = Image.new("RGB", (w, h), color)
        draw = ImageDraw.Draw(image)
        if i < 4:
            draw.rectangle((22, 20, w - 22, h - 20), fill="#f7f7f7")
            draw.rectangle((22, 20, w - 22, 74), fill="#282c3f")
            draw.text((45, 40), ["Photo library", "Chat window", "Desktop settings", "Monthly report"][i], fill="white")
            for row in range(4):
                y = 100 + row * 65
                draw.rounded_rectangle((55, y, w - 60 - row * 22, y + 42), radius=9, fill=color)
                draw.text((70, y + 15), f"Item {row + 1}", fill="white")
        else:
            draw.ellipse((w * .64, h * .08, w * .80, h * .28), fill="#ffe6a0")
            draw.polygon([(0, h * .73), (w * .25, h * .35), (w * .50, h * .75), (w * .78, h * .43), (w, h * .68), (w, h), (0, h)], fill="#273e42")
            draw.polygon([(0, h * .84), (w * .40, h * .66), (w, h * .84), (w, h), (0, h)], fill="#7ea88d")
        yield image


def compare(preset, images, sizes):
    import mlx.core as mx

    tagger._load_model(str(cached_model(preset)))
    tagger.runtime["model_id"] = tagger.MODEL_PRESETS[preset]
    try:
        for size in sizes:
            chunks = [list(enumerate(images[i:i + size])) for i in range(0, len(images), size)]
            tagger._infer_batch(chunks[0], False)  # warm the exact tested path
            mx.reset_peak_memory()
            start = perf_counter()
            rows = [item for chunk in chunks for item in tagger._infer_batch(chunk, True).values()]
            elapsed = perf_counter() - start
            print(json.dumps({
                "model": preset, "batch_size": size, "images": len(rows),
                "seconds": round(elapsed, 2), "images_per_minute": round(len(rows) * 60 / elapsed, 2),
                "json_success": sum(row["status"] == "success" for row in rows),
                "strict_json": sum(strict_object(row.get("raw_model_response", "")) for row in rows),
                "batched_images": sum(row["inference_batch_size"] > 1 for row in rows),
                "peak_mlx_gb": round(mx.get_peak_memory() / 1e9, 2),
            }), flush=True)
    finally:
        tagger._unload_model()
        gc.collect()


def strict_object(text):
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presets", nargs="+", choices=["4b", "3.5-4b"], default=["4b", "3.5-4b"])
    parser.add_argument("--batch-sizes", type=int, nargs="+", choices=range(1, 5), default=[1, 2])
    args = parser.parse_args()
    for preset in args.presets:
        cached_model(preset)
    images = list(samples())
    for preset in args.presets:
        compare(preset, images, args.batch_sizes)
