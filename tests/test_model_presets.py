"""Model catalog regression checks; no weights are loaded or downloaded."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "mac" if (ROOT / "mac").is_dir() else ROOT
REMOTE = ROOT / "remote_web" if (ROOT / "remote_web").is_dir() else ROOT.parent / "remote_web"
sys.path.insert(0, str(MAC))
import tagger_api

spec = importlib.util.spec_from_file_location("remote_catalog_app", REMOTE / "app.py")
remote = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = remote
spec.loader.exec_module(remote)


class ModelCatalogTests(unittest.TestCase):
    def test_qwen35_candidates_preserve_qwen3_support(self):
        presets = tagger_api.list_models()["presets"]
        self.assertEqual(presets["4b"], "mlx-community/Qwen3-VL-4B-Instruct-4bit")
        self.assertEqual(presets["8b"], "mlx-community/Qwen3-VL-8B-Instruct-4bit")
        self.assertEqual(presets["3.5-4b"], "mlx-community/Qwen3.5-4B-MLX-4bit")
        self.assertEqual(presets["3.5-9b"], "mlx-community/Qwen3.5-9B-MLX-4bit")

    def test_new_preset_is_listed_and_switchable(self):
        catalog = {"future-9b": "example/New-Vision-9B"}
        with patch.dict(tagger_api.MODEL_PRESETS, catalog, clear=True), \
                patch.dict(tagger_api.runtime, {"model_id": catalog["future-9b"]}, clear=True):
            self.assertEqual(tagger_api.list_models()["presets"], catalog)
            result = asyncio.run(tagger_api.switch_model(tagger_api.ModelSwitchRequest(preset=" FUTURE-9B ")))
            self.assertEqual(result, {"model": catalog["future-9b"], "status": "already_active"})

    def test_backend_rejects_unknown_preset_before_loading(self):
        with patch.dict(tagger_api.MODEL_PRESETS, {"future-9b": "example/New-Vision-9B"}, clear=True), \
                patch.object(tagger_api, "_load_model") as load:
            with self.assertRaises(tagger_api.HTTPException) as raised:
                asyncio.run(tagger_api.switch_model(tagger_api.ModelSwitchRequest(preset="unknown")))
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("future-9b", raised.exception.detail)
            load.assert_not_called()

    def test_proxy_relays_future_preset(self):
        payload = remote.ServiceRequest(service_url="http://localhost:8787", api_key="test-key", preset=" future-9b ")
        with patch.object(remote, "mac_request", return_value={"status": "active"}) as request:
            self.assertEqual(remote.model(payload), {"status": "active"})
            self.assertEqual(json.loads(request.call_args.args[4]), {"preset": "future-9b"})

    def test_proxy_rejects_empty_preset(self):
        with patch.object(remote, "mac_request") as request:
            with self.assertRaises(remote.HTTPException) as raised:
                remote.model(remote.ServiceRequest(service_url="http://localhost:8787", api_key="test-key"))
            self.assertEqual(raised.exception.status_code, 422)
            request.assert_not_called()

    def test_proxy_relays_backend_catalog_without_filtering(self):
        catalog = {"current_model": "example/New-Vision-9B", "presets": {"future-9b": "example/New-Vision-9B"}}
        with patch.object(remote, "mac_request", return_value=catalog):
            result = remote.models(remote.ServiceRequest(service_url="http://localhost:8787", api_key="test-key"))
            self.assertEqual(result, catalog)


if __name__ == "__main__":
    unittest.main()
