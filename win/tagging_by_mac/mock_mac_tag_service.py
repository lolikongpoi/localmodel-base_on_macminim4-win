"""仅用于 Windows 本地验证客户端；不运行任何 AI 模型，也不对外提供服务。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_KEY = "test-local-key"


class MockHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("X-API-Key") != API_KEY:
            self._json(401, {"detail": "invalid API key"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok", "model": "mock-qwen3-vl-4b"})
        elif self.path == "/v1/models" and self._authorized():
            self._json(200, {"current_model": "mock-qwen3-vl-4b", "presets": {"4b": "mock-qwen3-vl-4b", "8b": "mock-qwen3-vl-8b"}})
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/v1/tag":
            content_type = self.headers.get("Content-Type", "")
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if "multipart/form-data" not in content_type or b'name="image"' not in raw:
                self._json(400, {"detail": "missing image"})
                return
            self._json(200, {
                "category": "casual_photo",
                "tags": ["本地模拟", "测试图片", "示例标签"],
                "character_candidates": [{"name": "示例角色", "confidence": 0.82}],
                "contains_text": False,
                "summary": "这是本地测试服务生成的固定响应。",
                "model": "mock-qwen3-vl-4b",
            })
        elif self.path == "/v1/model":
            self._json(200, {"model": "mock-qwen3-vl-4b", "status": "switched"})
        else:
            self._json(404, {"detail": "not found"})

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    print("本地模拟服务：http://127.0.0.1:8787  API key: test-local-key")
    ThreadingHTTPServer(("127.0.0.1", 8787), MockHandler).serve_forever()

