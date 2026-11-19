"""テスト用の偽 llama-server。実物と同じ引数・環境変数・HTTP の振る舞いの一部だけを再現する。

- `--help` で対応引数を表示 / `--version`
- 環境変数 LLAMA_API_KEY で認証（Authorization: Bearer）
- /health は起動直後 0.5 秒だけ 503
- /v1/chat/completions: SSE でストリーム。プロンプトに OVERFLOW を含むと 400、SLOW を含むと遅く長く返す
- FAKE_MODE=crash で起動直後に異常終了 / FAKE_ARGS_OUT に受け取った引数をJSONで書き出す
- FAKE_REPLY に文字列があれば、それを応答にする（既定は「こんにちは世界。」）
- FAKE_CHUNKED=1 で、実物と同じ HTTP/1.1 の chunked 転送でストリームする（既定は接続クローズ区切り）
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELP = """usage: llama-server [options]
-t,    --threads N      number of CPU threads
-tb,   --threads-batch N
-c,    --ctx-size N     size of the prompt context
-np,   --parallel N     number of server slots
-m,    --model FNAME    model path
--host HOST             ip address to listen
--port PORT             port to listen
--api-key KEY           API key (env: LLAMA_API_KEY)
--no-webui              disable the built-in chat app
--no-slots              disable slots endpoint
-rea,  --reasoning [on|off|auto]
--chat-template-kwargs STRING
"""

args = sys.argv[1:]
if "--help" in args or "-h" in args:
    print(HELP)
    sys.exit(0)
if "--version" in args:
    print("version: fake (build 0)")
    sys.exit(0)
if os.environ.get("FAKE_ARGS_OUT"):
    with open(os.environ["FAKE_ARGS_OUT"], "w", encoding="utf-8") as f:
        json.dump(args, f)
if os.environ.get("FAKE_MODE") == "crash":
    print("fake: failed to load model", file=sys.stderr)
    sys.exit(3)

host = args[args.index("--host") + 1]
port = int(args[args.index("--port") + 1])
key = os.environ.get("LLAMA_API_KEY", "")
started = time.time()
CHUNKED = os.environ.get("FAKE_CHUNKED") == "1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1" if CHUNKED else "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _write(self, data: bytes):
        if CHUNKED:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    def _send_json(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {key}"

    def do_GET(self):
        if self.path == "/health":
            if time.time() - started < 0.5:
                return self._send_json(503, {"error": {"message": "Loading model"}})
            return self._send_json(200, {"status": "ok"})
        self._send_json(404, {})

    def do_POST(self):
        if not self._authorized():
            return self._send_json(401, {"error": {"code": 401, "message": "Invalid API Key"}})
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        prompt = payload["messages"][-1]["content"]
        if "OVERFLOW" in prompt:
            return self._send_json(400, {"error": {
                "code": 400, "type": "exceed_context_size_error",
                "message": "request (9999 tokens) exceeds the available context size (8192 tokens)"}})
        pieces = ["こんにちは", "世界", "。"]
        if os.environ.get("FAKE_REPLY"):
            pieces = [ln + "\n" for ln in os.environ["FAKE_REPLY"].split("\n")]
        delay = 0.0
        if "SLOW" in prompt:
            pieces, delay = ["あ"] * 200, 0.05
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if CHUNKED:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for p in pieces:
                chunk = {"choices": [{"delta": {"content": p}, "finish_reason": None}]}
                self._write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
                time.sleep(delay)
            last = {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "timings": {"prompt_n": 40, "prompt_ms": 400.0, "predicted_n": 3, "predicted_ms": 150.0, "cache_n": 10},
                "usage": {"prompt_tokens": 50, "completion_tokens": 3},
            }
            self._write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
            if CHUNKED:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


ThreadingHTTPServer((host, port), Handler).serve_forever()
