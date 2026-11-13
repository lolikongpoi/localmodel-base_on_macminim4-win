import csv
import io
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from test_model_presets import remote, tagger_api


def image_bytes():
    with io.BytesIO() as buffer:
        Image.new("RGB", (64, 64), "navy").save(buffer, format="PNG")
        return buffer.getvalue()


def broken_png_bytes():
    png = image_bytes()
    idat = png.index(b"IDAT")
    # Open succeeds, but the decoder encounters an invalid chunk after IDAT.
    chunk = b"IDAT" + png[idat + 4:idat + 5]
    return png[:idat - 4] + struct.pack(">I", 1) + chunk + struct.pack(">I", zlib.crc32(chunk)) + bytes(8)


class BatchEndpointTests(unittest.TestCase):
    def test_one_result_per_input_in_original_order(self):
        good = '{"category":"other","tags":["蓝色"],"summary":"示例"}'
        fake_mlx = SimpleNamespace(batch_generate=lambda *args, **kwargs: SimpleNamespace(texts=[good, "not JSON"]))
        with patch.dict(sys.modules, {"mlx_vlm": fake_mlx}), patch.dict(tagger_api.runtime, {
            "model": object(), "processor": object(), "model_id": "test-model",
        }), patch.object(tagger_api, "API_KEY", "test-key"):
            client = TestClient(tagger_api.app)
            response = client.post("/v1/tag/batch", headers={"X-API-Key": "test-key", "X-Debug-Raw-Model": "1"}, files=[
                ("images", ("good.png", image_bytes(), "image/png")),
                ("images", ("corrupt.png", broken_png_bytes(), "image/png")),
                ("images", ("non-json.png", image_bytes(), "image/png")),
            ])
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()["results"]
        self.assertEqual([row["index"] for row in rows], [0, 1, 2])
        self.assertEqual(rows[0]["result"]["tags"], ["蓝色"])
        self.assertEqual(rows[0]["inference_batch_size"], 2)
        self.assertEqual(rows[1]["status_code"], 415)
        self.assertEqual(rows[2]["status_code"], 502)
        self.assertEqual(rows[2]["raw_model_response"], "not JSON")

    def test_batch_limit(self):
        with patch.object(tagger_api, "API_KEY", "test-key"):
            response = TestClient(tagger_api.app).post(
                "/v1/tag/batch", headers={"X-API-Key": "test-key"},
                files=[("images", (f"{i}.png", b"x", "image/png")) for i in range(7)],
            )
        self.assertEqual(response.status_code, 422)

    def test_six_images_fit_the_new_pixel_budget(self):
        dims = SimpleNamespace(width=2048, height=2048)
        groups = list(tagger_api._image_batches(list(enumerate([dims] * 6))))
        self.assertEqual([len(group) for group in groups], [6])
        self.assertEqual(tagger_api.healthz()["max_batch_pixels"], 30_000_000)

    def test_broken_png_is_415_in_single_mode(self):
        png = broken_png_bytes()
        with Image.open(io.BytesIO(png)) as image, self.assertRaises(SyntaxError):
            image.load()
        with patch.object(tagger_api, "API_KEY", "test-key"):
            response = TestClient(tagger_api.app).post(
                "/v1/tag", headers={"X-API-Key": "test-key"},
                files={"image": ("corrupt.png", png, "image/png")},
            )
        self.assertEqual(response.status_code, 415)


class LiveRunTests(unittest.TestCase):
    def test_batch_http_500_retries_each_image_and_continues_next_batch(self):
        calls = []
        model_result = {"category": "other", "model": "test-model"}

        def upstream(url, key, method, endpoint, body=None, *args, **kwargs):
            calls.append(endpoint)
            if endpoint == "/healthz":
                return {"status": "ok", "max_batch_size": 6}
            if endpoint == "/v1/tag/batch":
                if calls.count(endpoint) == 1:
                    raise remote.HTTPException(500, "batch failed")
                return {"results": [
                    {"index": i, "status": "success", "result": model_result} for i in range(2)
                ]}
            if b'filename="0.png"' in body:
                raise remote.HTTPException(415, "broken image")
            return model_result

        with tempfile.TemporaryDirectory() as temp, patch.object(remote, "mac_request", side_effect=upstream):
            root = Path(temp)
            folder, output = root / "images", root / "reports"
            folder.mkdir()
            output.mkdir()
            for i in range(4):
                (folder / f"{i}.png").write_bytes(image_bytes())
            run = remote.Run("test", "http://localhost:8787", "test-key", folder, None, False,
                             True, None, None, False, False, output, batch_size=2)
            remote.worker(run)
            self.assertEqual(run.state, "finished", run.fatal_error)
            self.assertEqual([r.status for r in run.records], ["error", "success", "success", "success"])
            self.assertEqual(calls, ["/healthz", "/v1/tag/batch", "/v1/tag", "/v1/tag", "/v1/tag/batch"])
            self.assertEqual([Path(r.source).name for r in run.records], [f"{i}.png" for i in range(4)])

    def test_new_client_reattaches_and_reports_remain_per_image(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        model_result = {"category": "other", "tags": ["蓝色"], "model": "test-model"}

        def upstream(url, key, method, endpoint, body=None, *args, **kwargs):
            calls.append(endpoint)
            if endpoint == "/healthz":
                return {"status": "ok", "max_batch_size": 4}
            if endpoint == "/v1/tag/batch":
                self.assertEqual(body.count(b'name="images"'), 2)
                entered.set()
                self.assertTrue(release.wait(5))
                return {"results": [
                    {"index": 0, "status": "success", "result": model_result},
                    {"index": 1, "status": "error", "status_code": 502, "detail": "bad JSON", "raw_model_response": "debug text"},
                ]}
            self.assertEqual(endpoint, "/v1/tag")
            return model_result

        with tempfile.TemporaryDirectory() as temp, patch.dict(remote.runs, {}, clear=True), \
                patch.object(remote, "ACCESS_TOKEN", "access"), patch.object(remote, "mac_request", side_effect=upstream):
            root = Path(temp)
            source = root / "images"
            source.mkdir()
            for i in range(3):
                (source / f"{i}.png").write_bytes(image_bytes())
            headers = {"X-Remote-Access-Token": "access"}
            original = TestClient(remote.app)
            response = original.post("/api/runs", headers=headers, json={
                "service_url": "http://localhost:8787", "api_key": "test-key",
                "folder": str(source), "output_folder": str(root / "reports"),
                "batch_size": 2, "debug_model_response": True,
            })
            self.assertEqual(response.status_code, 201, response.text)
            run_id = response.json()["id"]
            refreshed = TestClient(remote.app)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(refreshed.get("/api/runs/active").status_code, 401)
                active = refreshed.get("/api/runs/active", headers=headers).json()["run"]
                self.assertEqual(active["id"], run_id)
                self.assertEqual(active["batch_size"], 2)
                self.assertEqual(active["state"], "running")
                self.assertNotIn("api_key", active)
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = refreshed.get(f"/api/runs/{run_id}", headers=headers).json()
                if status["state"] in {"finished", "failed"}:
                    break
                time.sleep(.01)
            self.assertEqual(status["state"], "finished", status)
            self.assertEqual([row["status"] for row in status["records"]], ["success", "error", "success"])
            self.assertEqual(refreshed.get("/api/runs/active", headers=headers).json(), {"run": None})
            report = Path(status["report_folder"])
            journal = [json.loads(line) for line in (report / "image_tags.jsonl").read_text().splitlines()]
            self.assertEqual([Path(row["source"]).name for row in journal], ["0.png", "1.png", "2.png"])
            self.assertTrue(all("raw_model_response" not in row for row in journal))
            with (report / "image_tags.csv").open(encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 3)
            self.assertEqual(calls, ["/healthz", "/v1/tag/batch", "/v1/tag"])


if __name__ == "__main__":
    unittest.main()
