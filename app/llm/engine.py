"""llama.cpp の llama-server を起動・監視・終了し、ローカルHTTPで呼び出す（3.2 / S-04）。

安全面の方針
- 127.0.0.1 以外では待ち受けない（Config.validate と、ここでの再確認）
- 空きポートとAPIキーは起動のたびにランダム。キーはコマンドラインではなく環境変数で渡す
- Web UI とスロット監視を無効化（対応している場合）
- 親プロセスが異常終了してもサーバーが残らないよう、Windows では Job Object に入れる
- サーバーの標準出力は破棄せずリングバッファに保持するだけで、ファイルには書かない（S-02）

通信は標準ライブラリの http.client のみ。環境のプロキシ設定の影響を受けず、外部にも出ない。
"""
from __future__ import annotations

import atexit
import collections
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.cancel import CancelToken
from app.core.config import LOOPBACK, Config, is_frozen
from app.core.errors import Cancelled, ContextOverflowError, EngineError, ServerStartError

try:  # RAM計測は任意（無ければ「計測不可」と表示する）
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

REQUIRED_FLAGS = ("-m", "--host", "--port", "--api-key", "-c", "-t", "-np")


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0         # プロンプト全体
    prompt_eval_tokens: int = 0    # 実際に評価した分（キャッシュ再利用を除く）
    completion_tokens: int = 0
    prompt_s: float = 0.0
    gen_s: float = 0.0
    finish_reason: str | None = None


def detect_flags(exe: Path, timeout: float = 30.0) -> set[str]:
    """`llama-server --help` から対応している引数名を集める（版による差異を吸収する）。"""
    try:
        proc = subprocess.run(
            [str(exe), "--help"],
            capture_output=True,
            timeout=timeout,
            cwd=str(exe.parent),
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ServerStartError(f"llama-server を実行できませんでした: {e}") from e
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", text))


def build_command(
    exe: Path, model: Path, port: int, cfg: Config, flags: set[str]
) -> list[str]:
    """起動コマンドを組み立てる。APIキーは含めない（環境変数 LLAMA_API_KEY で渡す）。"""
    missing = [f for f in REQUIRED_FLAGS if f not in flags]
    if missing:
        raise ServerStartError(
            "この llama-server は必要な引数に対応していません: " + ", ".join(missing)
            + "。" + ("配布zipに同梱の llama フォルダを、そのまま使ってください（別の版に差し替えていませんか）。" if is_frozen()
                      else "`python tools/setup_assets.py llama` で動作確認済みの版を取得してください。")
        )
    cmd = [
        str(exe), "-m", str(model),
        "--host", LOOPBACK, "--port", str(port),
        "-c", str(cfg.ctx_size), "-t", str(cfg.threads), "-np", "1",
    ]
    if cfg.threads_batch and "-tb" in flags:
        cmd += ["-tb", str(cfg.threads_batch)]
    if "--no-webui" in flags:
        cmd.append("--no-webui")
    if "--no-slots" in flags:
        cmd.append("--no-slots")
    if "--reasoning" in flags:                      # Qwen3 を非思考モードで使う
        cmd += ["--reasoning", "off"]
    if "--chat-template-kwargs" in flags:
        cmd += ["--chat-template-kwargs", json.dumps({"enable_thinking": False})]
    return cmd


def _creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return 0x08000000 | 0x00000200   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def _attach_kill_on_close_job(proc: subprocess.Popen):
    """Windows: 親（このアプリ）が終了したら子（llama-server）も必ず終了するようにする。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
            k32.CloseHandle(job)
            return None
        return (k32, job)
    except Exception:
        return None


class LlamaServerEngine:
    """llama-server のライフサイクルと推論呼び出し。with 文で使える。"""

    def __init__(self, cfg: Config, model_path: Path, server_path: Path) -> None:
        cfg.validate()
        self.cfg = cfg
        self.model_path = Path(model_path)
        self.server_path = Path(server_path)
        self.host = LOOPBACK
        self.port = 0
        self._key = ""
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._log: collections.deque[str] = collections.deque(maxlen=200)
        self._peak_rss = 0
        self._stop_sampler = threading.Event()
        self._started = False
        self.startup_s = 0.0

    # ------------------------------------------------------------ ライフサイクル

    def __enter__(self) -> "LlamaServerEngine":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def peak_rss_mb(self) -> float | None:
        return self._peak_rss / (1024 * 1024) if self._peak_rss else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, cancel: CancelToken | None = None) -> float:
        """サーバーを起動し、利用可能になるまで待つ。起動に要した秒数を返す。"""
        if self.is_alive():
            return 0.0
        if not self.model_path.is_file():
            raise ServerStartError(f"モデルファイルが見つかりません: {self.model_path.name}")
        flags = detect_flags(self.server_path)
        self._stop_sampler.clear()
        self._peak_rss = 0
        self.port = _free_port()
        self._key = secrets.token_urlsafe(32)
        cmd = build_command(self.server_path, self.model_path, self.port, self.cfg, flags)
        env = dict(os.environ)
        env["LLAMA_API_KEY"] = self._key

        t0 = time.perf_counter()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(self.server_path.parent),
                creationflags=_creationflags(),
            )
        except OSError as e:
            raise ServerStartError(f"llama-server を起動できませんでした: {e}") from e
        self._job = _attach_kill_on_close_job(self._proc)
        atexit.register(self.stop)
        threading.Thread(target=self._drain, daemon=True).start()
        try:
            self._wait_ready(t0, cancel)
        except BaseException:
            self.stop()
            raise
        self._started = True
        if psutil is not None:
            threading.Thread(target=self._sample_rss, daemon=True).start()
        self.startup_s = time.perf_counter() - t0
        return self.startup_s

    def stop(self) -> None:
        self._stop_sampler.set()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if self._job is not None:
            k32, job = self._job
            try:
                k32.CloseHandle(job)
            except Exception:
                pass
            self._job = None

    def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                self._log.append(raw.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def _sample_rss(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            p = psutil.Process(proc.pid)
            while not self._stop_sampler.wait(0.5):
                info = p.memory_info()
                peak = getattr(info, "peak_wset", 0) or 0   # Windows ではピーク値が直接取れる
                self._peak_rss = max(self._peak_rss, info.rss, peak)
        except Exception:
            pass

    def _wait_ready(self, t0: float, cancel: CancelToken | None) -> None:
        deadline = t0 + self.cfg.startup_timeout_s
        while True:
            if cancel is not None:
                cancel.raise_if_set()
            if self._proc is None or self._proc.poll() is not None:
                code = self._proc.returncode if self._proc else "?"
                raise ServerStartError(
                    f"llama-server が起動中に終了しました（終了コード {code}）。\n" + self.log_tail()
                )
            status = self._health()
            if status == 200:
                return
            if time.perf_counter() > deadline:
                raise ServerStartError(
                    f"llama-server の起動が {self.cfg.startup_timeout_s:.0f} 秒以内に完了しませんでした。\n" + self.log_tail()
                )
            time.sleep(0.2)

    def _health(self) -> int | None:
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
            try:
                conn.request("GET", "/health", headers={"Authorization": f"Bearer {self._key}"})
                return conn.getresponse().status
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            return None

    def log_tail(self, n: int = 12) -> str:
        return "\n".join(list(self._log)[-n:])

    # ------------------------------------------------------------ 推論

    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        cancel: CancelToken | None = None,
    ) -> ChatResult:
        if not self.is_alive():
            raise EngineError("推論サーバーが起動していません。")
        cancel = cancel or CancelToken()
        cancel.raise_if_set()
        c = self.cfg
        payload = {
            "model": "local",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": c.temperature,
            "top_p": c.top_p,
            "top_k": c.top_k,
            "min_p": c.min_p,
            "presence_penalty": c.presence_penalty,
            "stream": True,
            "stream_options": {"include_usage": True},
            "cache_prompt": c.cache_prompt,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"}
        conn = http.client.HTTPConnection(self.host, self.port, timeout=c.request_timeout_s)
        sock: socket.socket | None = None
        remove = lambda: None  # noqa: E731
        t0 = time.perf_counter()
        try:
            conn.connect()
            # 応答がクローズ区切りの場合 http.client は conn.sock を手放すため、中止用に自前で保持しておく
            sock = conn.sock
            remove = cancel.add_callback(lambda: self._abort(sock))
            conn.request("POST", "/v1/chat/completions", body, headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise self._http_error(resp.status, resp.read())
            return self._consume(resp, cancel, t0)
        except (OSError, http.client.HTTPException) as e:
            if cancel.is_set():
                raise Cancelled("処理が中止されました。") from e
            if not self.is_alive():
                raise EngineError("推論サーバーが予期せず終了しました。\n" + self.log_tail()) from e
            raise EngineError(f"推論サーバーとの通信に失敗しました: {e}") from e
        finally:
            remove()
            conn.close()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _abort(sock: socket.socket | None) -> None:
        """別スレッドから通信を切断する。読み込み待ちのスレッドは即座に戻る。"""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @staticmethod
    def _http_error(status: int, raw: bytes) -> EngineError:
        try:
            err = json.loads(raw.decode("utf-8", errors="replace")).get("error", {})
        except (ValueError, AttributeError):
            err = {}
        return LlamaServerEngine._error_from(status, err)

    @staticmethod
    def _error_from(status: int, err: dict) -> EngineError:
        kind = str(err.get("type", ""))
        msg = str(err.get("message", "")) or f"HTTP {status}"
        if "exceed_context" in kind or "exceeds the available context" in msg:
            return ContextOverflowError("入力がコンテキスト長を超えました。")
        if status == 401:
            return EngineError("推論サーバーの認証に失敗しました。")
        return EngineError(f"推論サーバーがエラーを返しました: {msg}")

    def _consume(self, resp: http.client.HTTPResponse, cancel: CancelToken, t0: float) -> ChatResult:
        parts: list[str] = []
        timings: dict = {}
        usage: dict = {}
        finish: str | None = None
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if "error" in obj:
                err = obj["error"] if isinstance(obj["error"], dict) else {"message": str(obj["error"])}
                raise self._error_from(int(err.get("code", 500) or 500), err)
            if obj.get("timings"):
                timings = obj["timings"]
            if obj.get("usage"):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if choices:
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
                finish = choices[0].get("finish_reason") or finish
        if cancel.is_set():
            raise Cancelled("処理が中止されました。")
        wall = time.perf_counter() - t0
        prompt_ms = float(timings.get("prompt_ms", 0.0))
        pred_ms = float(timings.get("predicted_ms", 0.0))
        eval_n = int(timings.get("prompt_n", usage.get("prompt_tokens", 0)) or 0)
        total_prompt = int(usage.get("prompt_tokens", 0) or (eval_n + int(timings.get("cache_n", 0) or 0)))
        return ChatResult(
            text="".join(parts),
            prompt_tokens=total_prompt,
            prompt_eval_tokens=eval_n,
            completion_tokens=int(usage.get("completion_tokens", timings.get("predicted_n", 0)) or 0),
            prompt_s=prompt_ms / 1000.0 if prompt_ms else 0.0,
            gen_s=pred_ms / 1000.0 if pred_ms else max(0.0, wall),
            finish_reason=finish,
        )
