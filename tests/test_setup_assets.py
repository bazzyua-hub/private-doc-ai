import hashlib
import http.server
import socketserver
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import setup_assets as sa  # noqa: E402

# 実際の llama.cpp リリース（b12503）の配布ファイル名から抜粋
RELEASE = {"tag_name": "b12503", "assets": [
    {"name": "llama-b12503-bin-win-cuda-12.4-x64.zip"},
    {"name": "llama-b12503-bin-win-cpu-arm64.zip"},
    {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip"},
    {"name": "llama-b12503-bin-win-cpu-x64.zip", "digest": "sha256:abc", "browser_download_url": "https://example.invalid/x.zip"},
    {"name": "llama-b12503-bin-ubuntu-x64.tar.gz"},
]}


def test_pick_llama_asset_selects_windows_cpu_x64_only():
    assert sa.pick_llama_asset(RELEASE)["name"] == "llama-b12503-bin-win-cpu-x64.zip"
    assert sa.pick_llama_asset({"assets": [{"name": "llama-b1-bin-win-cpu-arm64.zip"}]}) is None
    assert sa.pick_llama_asset({"assets": []}) is None


SIBLINGS = [{"rfilename": n} for n in [
    "README.md", "Qwen3-4B-Q4_K_M.gguf", "Qwen3-4B-Q5_0.gguf", "Qwen3-4B-Q8_0.gguf",
    "mmproj-Qwen3-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M-00001-of-00002.gguf",
]]


def test_pick_hf_file():
    assert sa.pick_hf_file(SIBLINGS, "Q4_K_M")["rfilename"] == "Qwen3-4B-Q4_K_M.gguf"
    assert sa.pick_hf_file(SIBLINGS, "q8_0")["rfilename"] == "Qwen3-4B-Q8_0.gguf"
    with pytest.raises(sa.SetupError, match="利用可能"):
        sa.pick_hf_file(SIBLINGS, "Q2_K")


def test_safe_extract_blocks_zip_slip(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escaped.txt", "x")
    with pytest.raises(sa.SetupError, match="不正なパス"):
        sa.safe_extract(evil, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()

    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("llama-server.exe", "bin")
        z.writestr("ggml.dll", "dll")
    names = sa.safe_extract(good, tmp_path / "out")
    assert sorted(names) == ["ggml.dll", "llama-server.exe"]
    assert (tmp_path / "out" / "llama-server.exe").read_bytes() == b"bin"


@pytest.fixture
def local_http(tmp_path):
    (tmp_path / "srv").mkdir()
    data = b"GGUF" + b"0123456789" * 50_000
    (tmp_path / "srv" / "model.gguf").write_bytes(data)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path / "srv"), **k)

        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/model.gguf", data
    httpd.shutdown()


def test_download_verifies_hash_and_cleans_up(local_http, tmp_path):
    url, data = local_http
    good = hashlib.sha256(data).hexdigest()
    dest = sa.download(url, tmp_path / "models" / "m.gguf", sha256=good)
    assert dest.read_bytes() == data and not (tmp_path / "models" / "m.gguf.part").exists()
    # 取得済みで一致するなら再ダウンロードしない
    assert sa.download("http://127.0.0.1:9/never", dest, sha256=good) == dest

    with pytest.raises(sa.SetupError, match="ハッシュ"):
        sa.download(url, tmp_path / "models" / "bad.gguf", sha256="0" * 64)
    assert not (tmp_path / "models" / "bad.gguf").exists()          # 壊れたファイルは残さない
    assert not (tmp_path / "models" / "bad.gguf.part").exists()
