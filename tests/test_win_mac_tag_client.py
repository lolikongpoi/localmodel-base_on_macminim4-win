import email.policy
import csv
import io
import json
import queue
import sys
import tempfile
import threading
import unittest
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "win" / "tagging_by_mac"))
from mac_tag_client import MacTagService, TagServiceError, tag_batch_records
from mac_tag_app import MacTagApp


class WindowsTagClientTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = [Path(directory.name) / f"{i}.jpg" for i in range(6)]
        for path in self.paths:
            path.write_bytes(path.name.encode())
        self.client = MacTagService("http://mac.test:8787", "test-key")

    def test_backend_supplied_preset_can_be_selected(self):
        def respond(request, timeout):
            self.assertEqual(request.get_header("X-api-key"), "test-key")
            if request.method == "GET":
                self.assertTrue(request.full_url.endswith("/v1/models"))
                return io.BytesIO(b'{"presets":{"future-vision":"model/future"}}')
            self.assertTrue(request.full_url.endswith("/v1/model"))
            self.assertEqual(json.loads(request.data), {"preset": "future-vision"})
            self.assertEqual(timeout, 600)
            return io.BytesIO(b'{"model":"model/future"}')

        with patch("mac_tag_client.urlopen", side_effect=respond):
            preset = next(iter(self.client.models()["presets"]))
            self.assertEqual(self.client.switch_model(preset)["model"], "model/future")

    def test_six_images_in_order_with_one_bad_image(self):
        def respond(request, timeout):
            self.assertTrue(request.full_url.endswith("/v1/tag/batch"))
            header = f"Content-Type: {request.get_header('Content-type')}\r\n\r\n"
            parts = list(BytesParser(policy=email.policy.default).parsebytes(
                header.encode() + request.data
            ).iter_parts())
            self.assertEqual([part.get_param("name", header="Content-Disposition") for part in parts],
                             ["images"] * 6)
            self.assertEqual([part.get_filename() for part in parts], [path.name for path in self.paths])
            results = [{"index": i, "status": "success", "result": {"summary": str(i)}} for i in range(6)]
            results[2] = {"index": 2, "status": "error", "status_code": 415, "detail": "bad image"}
            return io.BytesIO(json.dumps({"results": results}).encode())

        with patch("mac_tag_client.urlopen", side_effect=respond):
            records = list(tag_batch_records(self.client, self.paths))
        self.assertEqual([record.source for record in records], [str(path) for path in self.paths])
        self.assertEqual([record.status for record in records], ["success", "success", "error"] + ["success"] * 3)
        self.assertEqual(records[3].summary, "3")

    def test_batch_failure_falls_back_to_single_image_requests(self):
        def respond(request, timeout):
            if request.full_url.endswith("/v1/tag/batch"):
                raise HTTPError(request.full_url, 503, "batch failed", {}, io.BytesIO())
            self.assertTrue(request.full_url.endswith("/v1/tag"))
            self.assertEqual(timeout, 180)
            return io.BytesIO(b'{"category":"other"}')

        with patch("mac_tag_client.urlopen", side_effect=respond) as request:
            self.assertEqual(len(list(tag_batch_records(self.client, self.paths))), 6)
            self.assertEqual(request.call_count, 7)

    def test_authentication_failure_does_not_retry_individually(self):
        error = TagServiceError(401, "unauthorized")
        with patch.object(self.client, "tag_batch", side_effect=error):
            with patch.object(self.client, "tag") as single:
                with self.assertRaises(TagServiceError):
                    list(tag_batch_records(self.client, self.paths))
                single.assert_not_called()

    def test_windows_worker_writes_each_record_and_honors_stop_between_batches(self):
        for server_limit in (1, 2):
            with self.subTest(server_limit=server_limit):
                state = SimpleNamespace(
                    events=queue.Queue(), records=[],
                    cancel_event=threading.Event(), pause_event=threading.Event(),
                )
                state.pause_event.set()
                service = Mock(base_url="http://mac.test:8787")
                service.health.return_value = {"status": "ok", "max_batch_size": server_limit}

                def identify(path):
                    state.cancel_event.set()
                    return {"summary": path.name}

                def batch(paths):
                    return [{"status": "success", "result": identify(path)} for path in paths]

                service.tag.side_effect = identify
                service.tag_batch.side_effect = batch
                folder = self.paths[0].parent
                MacTagApp._tag_worker(state, service, folder, folder, self.paths, None, 6)
                events = list(state.events.queue)
                self.assertEqual(events[-1][0], "finished", events)
                run_dir, reason, planned, processed = events[-1][1]
                self.assertEqual((reason, planned, processed), ("user_stop", 6, server_limit))
                with (run_dir / "image_tags.csv").open(encoding="utf-8-sig", newline="") as handle:
                    self.assertEqual([row["source"] for row in csv.DictReader(handle)],
                                     [str(path) for path in self.paths[:server_limit]])


if __name__ == "__main__":
    unittest.main()
