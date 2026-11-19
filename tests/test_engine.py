"""推論エンジンのテスト。偽の llama-server を実際にプロセスとして起動して、起動・通信・停止を確認する。

実物のモデルでの動作確認ではない（実機での確認は README の手順を参照）。
"""
import http.client
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.cancel import CancelToken
from app.core.config import Config
from app.core.errors import Cancelled, ContextOverflowError, ServerStartError
from app.llm.engine import REQUIRED_FLAGS, LlamaServerEngine, build_command, detect_flags

FAKE = Path(__file__).parent / "fake_llama_server.py"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="偽サーバーの起動ラッパーは POSIX 用")


@pytest.fixture
def fake_server(tmp_path):
    wrapper = tmp_path / "llama-server"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "Qwen3-1.7B-Q8_0.gguf"
    model.write_bytes(b"GGUF")
    return wrapper, model


def make_engine(fake_server, **kw):
    exe, model = fake_server
    cfg = Config(startup_timeout_s=20, request_timeout_s=20, **kw).validate()
    return LlamaServerEngine(cfg, model, exe)


# ------------------------------------------------------------ コマンド組み立て（全OS）

ALL_FLAGS = {"-m", "--host", "--port", "--api-key", "-c", "-t", "-np", "-tb", "--no-webui", "--no-slots",
             "--reasoning", "--chat-template-kwargs"}


def test_build_command_is_loopback_only_and_hides_api_key(tmp_path):
    cfg = Config(threads=6, threads_batch=8, ctx_size=8192).validate()
    cmd = build_command(Path("llama-server"), tmp_path / "m.gguf", 12345, cfg, ALL_FLAGS)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "--api-key" not in cmd                         # キーはコマンドラインに出さない（環境変数で渡す）
    assert cmd[cmd.index("-np") + 1] == "1"
    assert cmd[cmd.index("-t") + 1] == "6" and cmd[cmd.index("-tb") + 1] == "8"
    assert cmd[cmd.index("-c") + 1] == "8192"
    assert "--no-webui" in cmd and "--no-slots" in cmd
    assert cmd[cmd.index("--reasoning") + 1] == "off"      # 非思考モード
    assert json.loads(cmd[cmd.index("--chat-template-kwargs") + 1]) == {"enable_thinking": False}


def test_build_command_skips_unsupported_optional_flags(tmp_path):
    flags = set(REQUIRED_FLAGS)
    cmd = build_command(Path("x"), tmp_path / "m.gguf", 1, Config().validate(), flags)
    assert "--no-webui" not in cmd and "--reasoning" not in cmd


def test_build_command_rejects_missing_required_flag(tmp_path):
    flags = set(REQUIRED_FLAGS) - {"--api-key"}
    with pytest.raises(ServerStartError, match="--api-key"):
        build_command(Path("x"), tmp_path / "m.gguf", 1, Config().validate(), flags)


def test_config_rejects_non_loopback_host():
    with pytest.raises(ValueError):
        Config(host="0.0.0.0").validate()
    with pytest.raises(ValueError):
        Config(host="192.168.0.5").validate()


def test_config_rejects_inconsistent_context_budget():
    with pytest.raises(ValueError, match="ctx_size"):
        Config(ctx_size=2048).validate()


# ------------------------------------------------------------ 実プロセスでの確認（POSIX）

@posix_only
def test_detect_flags_reads_help(fake_server):
    flags = detect_flags(fake_server[0])
    assert set(REQUIRED_FLAGS) <= flags and "--no-webui" in flags


@posix_only
@pytest.mark.parametrize("chunked", ["", "1"], ids=["close-delimited", "chunked"])
def test_lifecycle_chat_and_cleanup(fake_server, tmp_path, monkeypatch, chunked):
    args_out = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS_OUT", str(args_out))
    monkeypatch.setenv("FAKE_CHUNKED", chunked)
    eng = make_engine(fake_server, threads=2)
    with eng:
        assert eng.startup_s > 0.3                         # 503 の間は待っている
        res = eng.chat("sys", "user", max_tokens=16)
        assert res.text == "こんにちは世界。"
        assert (res.prompt_tokens, res.prompt_eval_tokens, res.completion_tokens) == (50, 40, 3)
        assert res.prompt_s == pytest.approx(0.4) and res.gen_s == pytest.approx(0.15)
        assert res.finish_reason == "stop"
        proc = eng._proc
        port = eng.port
    assert proc.poll() is not None                          # with を抜けたらプロセスは終了している
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/health")

    args = json.loads(args_out.read_text())
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--api-key" not in args and "--no-webui" in args


@posix_only
def test_api_key_is_required_and_random(fake_server):
    e1, e2 = make_engine(fake_server), make_engine(fake_server)
    with e1, e2:
        assert e1._key != e2._key and e1.port != e2.port and len(e1._key) >= 32
        conn = http.client.HTTPConnection("127.0.0.1", e1.port, timeout=5)
        conn.request("POST", "/v1/chat/completions", b"{}", {"Authorization": "Bearer wrong"})
        assert conn.getresponse().status == 401
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", e1.port, timeout=5)
        conn.request("POST", "/v1/chat/completions", b"{}", {"Authorization": f"Bearer {e2._key}"})  # 他のサーバーの鍵も通らない
        assert conn.getresponse().status == 401
        conn.close()


@posix_only
def test_context_overflow_is_mapped_to_typed_error(fake_server):
    with make_engine(fake_server) as eng:
        with pytest.raises(ContextOverflowError):
            eng.chat("sys", "OVERFLOW", max_tokens=16)
        assert eng.chat("sys", "ok", max_tokens=16).text                # エラー後も使える


@posix_only
@pytest.mark.parametrize("chunked", ["", "1"], ids=["close-delimited", "chunked"])
def test_cancel_during_generation(fake_server, monkeypatch, chunked):
    monkeypatch.setenv("FAKE_CHUNKED", chunked)
    with make_engine(fake_server) as eng:
        cancel = CancelToken()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.perf_counter()
        with pytest.raises(Cancelled):
            eng.chat("sys", "SLOW", max_tokens=500, cancel=cancel)     # 通常なら約10秒かかる応答
        assert time.perf_counter() - t0 < 5
        assert eng.chat("sys", "ok", max_tokens=16).text                # 中止後も次の呼び出しができる


@posix_only
def test_cancel_before_call(fake_server):
    with make_engine(fake_server) as eng:
        cancel = CancelToken()
        cancel.set()
        with pytest.raises(Cancelled):
            eng.chat("s", "u", max_tokens=8, cancel=cancel)


@posix_only
def test_startup_crash_reports_log(fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    with pytest.raises(ServerStartError, match="failed to load model"):
        make_engine(fake_server).start()


@posix_only
def test_missing_model_file(fake_server):
    exe, model = fake_server
    model.unlink()
    with pytest.raises(ServerStartError, match="モデルファイル"):
        make_engine(fake_server).start()


@posix_only
def test_server_dies_unexpectedly_is_reported(fake_server):
    eng = make_engine(fake_server)
    eng.start()
    try:
        eng._proc.kill()
        eng._proc.wait()
        from app.core.errors import EngineError
        with pytest.raises(EngineError):
            eng.chat("s", "u", max_tokens=8)
    finally:
        eng.stop()


@posix_only
def test_stop_is_idempotent_and_restart_works(fake_server):
    eng = make_engine(fake_server)
    eng.start()
    eng.stop()
    eng.stop()
    eng.start()
    try:
        assert eng.chat("s", "u", max_tokens=8).text
    finally:
        eng.stop()
