"""配布版（PyInstaller・zip）に関するテスト。

実際の exe の生成は PyInstaller が必要で時間がかかるため、ここでは「凍結時のパス解決」「zip の中身」「同梱物の
そろい方」「ソースコードzipが余計なものを含まないこと」を確かめる。exe の起動確認は tools/build_package.py の自己診断が行う。
"""
import ast
import struct
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_package as bp  # noqa: E402
import make_icon  # noqa: E402

from app.core import config  # noqa: E402
from app.core.errors import AssetNotFoundError  # noqa: E402


# ------------------------------------------------------------ 凍結時のパス

def test_app_root_is_the_project_folder_in_development():
    assert not config.is_frozen()
    assert config.app_root() == ROOT


def test_app_root_is_the_exe_folder_when_frozen(monkeypatch, tmp_path):
    exe = tmp_path / "LocalDocAI" / "LocalDocAI.exe"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert config.is_frozen()
    assert config.app_root() == exe.parent.resolve()          # __file__（_internal の中）ではなく、exe の隣


def test_missing_assets_message_tells_the_frozen_user_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = config.Config(models_dir=tmp_path / "models", llama_dir=tmp_path / "llama")
    with pytest.raises(AssetNotFoundError) as e:
        config.resolve_model(cfg)
    assert "get_model.bat" in str(e.value) and "python" not in str(e.value)       # 配布先には Python も tools も無い
    monkeypatch.setenv("PATH", str(tmp_path))                                     # PATH 上の llama-server も無い状態
    with pytest.raises(AssetNotFoundError) as e:
        config.resolve_server(cfg)
    assert "llama フォルダ" in str(e.value) and "setup_assets" not in str(e.value)


def test_missing_assets_message_for_developers_still_names_the_setup_script(tmp_path):
    cfg = config.Config(models_dir=tmp_path / "models", llama_dir=tmp_path / "llama")
    with pytest.raises(AssetNotFoundError) as e:
        config.resolve_model(cfg)
    assert "setup_assets.py model" in str(e.value)


def test_frozen_layout_finds_assets_next_to_the_exe(monkeypatch, tmp_path):
    """配布フォルダの並び（exe / llama/ / models/）で、モデルと llama-server が見つかる。"""
    (tmp_path / "llama").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "llama" / config.SERVER_EXE).write_text("")
    (tmp_path / "models" / "Qwen3-1.7B-Q8_0.gguf").write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "LocalDocAI.exe"))
    cfg = config.Config(models_dir=config.app_root() / "models", llama_dir=config.app_root() / "llama")
    assert config.resolve_server(cfg).name == config.SERVER_EXE
    assert config.resolve_model(cfg).name == "Qwen3-1.7B-Q8_0.gguf"


# ------------------------------------------------------------ 起動確認（--selftest）

def test_selftest_reports_without_creating_files(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from app.gui.app import selftest
    code, text = selftest()
    assert code == 0 and "RESULT OK" in text and "PyMuPDF" in text and "フォント" in text


def test_gui_entry_writes_the_selftest_report_where_asked(tmp_path):
    """起動口（gui.py）が報告を書く。app/ の中はファイルを書かない方針（test_security）を守るため。"""
    import os
    import subprocess
    report = tmp_path / "report.txt"
    r = subprocess.run([sys.executable, str(ROOT / "gui.py"), "--selftest", str(report)], cwd=ROOT, timeout=120,
                       env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert r.returncode == 0
    assert "RESULT OK" in report.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.txt"]          # それ以外のファイルを作らない


# ------------------------------------------------------------ 同梱物

def test_packaging_files_exist():
    for rel in ("packaging/LocalDocAI.spec", "packaging/LocalDocAI.ico", "packaging/THIRD_PARTY_NOTICES.txt",
                "packaging/licenses/LGPL-3.0.txt", "packaging/licenses/Apache-2.0.txt", "packaging/licenses/MIT-llama.cpp.txt",
                "packaging/dist_files/使い方.txt", "packaging/dist_files/get_model.bat", "packaging/dist_files/get_model.ps1",
                "packaging/dist_files/check.bat", "requirements-build.txt", "build.bat", "LICENSE"):
        assert (ROOT / rel).is_file(), rel


def test_spec_bundles_fonts_and_leaves_out_network_parts():
    src = (ROOT / "packaging/LocalDocAI.spec").read_text(encoding="utf-8")
    ast.parse(src)
    assert "app/gui/assets/fonts" in src and "console=False" in src
    assert "PySide6.QtNetwork" in src and "upx=False" in src
    for f in ("HachiMaruPop-Regular.ttf", "ZenMaruGothic-Regular.ttf", "ZenMaruGothic-Bold.ttf", "OFL-HachiMaruPop.txt", "OFL-ZenMaruGothic.txt"):
        assert (ROOT / "app/gui/assets/fonts" / f).is_file()


def test_notices_cover_every_bundled_component():
    text = (ROOT / "packaging/THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    for name in ("PySide6", "PyMuPDF", "psutil", "PyInstaller", "llama.cpp", "Qwen3", "Hachi Maru Pop", "Zen Maru Gothic", "AGPL-3.0", "LGPL-3.0"):
        assert name in text, name


def test_batch_files_are_ascii_with_crlf_and_the_model_script_has_a_bom():
    for name in ("get_model.bat", "check.bat"):
        raw = (ROOT / "packaging/dist_files" / name).read_bytes()
        raw.decode("ascii")                                                        # 文字化けしないよう、ASCIIだけ
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    ps1 = (ROOT / "packaging/dist_files/get_model.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")                                          # Windows PowerShell 5.1 で日本語を読むための BOM
    body = ps1.decode("utf-8-sig")
    assert "huggingface.co" in body and "SHA256" in body and "http://" not in body


def test_check_bat_switches_to_utf8_before_showing_the_report():
    """報告はUTF-8で書かれる。コードページを切り替えないと、コンソールで文字化けする（実機で確認）。"""
    bat = (ROOT / "packaging/dist_files/check.bat").read_text(encoding="ascii")
    assert bat.index("chcp 65001") < bat.index("type ")


def test_icon_file_is_a_valid_multi_size_ico():
    raw = (ROOT / "packaging/LocalDocAI.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind, count) == (0, 1, len(make_icon.SIZES))
    first_off = struct.unpack("<I", raw[6 + 12:6 + 16])[0]
    assert raw[first_off:first_off + 8] == b"\x89PNG\r\n\x1a\n"


def test_build_ico_layout():
    ico = make_icon.build_ico([(16, b"a" * 10), (256, b"b" * 20)])
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 2)
    assert ico[6] == 16 and ico[6 + 16] == 0                                        # 256 は幅0と書く決まり
    assert ico.endswith(b"a" * 10 + b"b" * 20)


# ------------------------------------------------------------ zip の組み立て

def _fake_tree(root: Path):
    (root / "app").mkdir()
    (root / "app" / "x.py").write_text("print(1)")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "x.pyc").write_text("c")
    (root / "models").mkdir()
    (root / "models" / "m.gguf").write_text("model")
    (root / "llama").mkdir()
    (root / "llama" / "llama-server.exe").write_text("bin")
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("x")
    (root / "samples").mkdir()
    (root / "samples" / "my.pdf").write_text("private")
    (root / "result.csv").write_text("a,b")
    (root / "LICENSE").write_text("AGPL")


def test_source_zip_excludes_models_binaries_and_private_files(tmp_path):
    _fake_tree(tmp_path)
    names = [p.as_posix() for p in bp.source_files(tmp_path)]
    assert names == ["LICENSE", "app/x.py"]
    z = bp.make_source_zip(tmp_path, tmp_path / "out" / "src.zip")
    with zipfile.ZipFile(z) as f:
        assert sorted(f.namelist()) == ["LocalDocAI-source/LICENSE", "LocalDocAI-source/app/x.py"]


def test_source_zip_never_contains_pdfs(tmp_path):
    """実機で確認：プロジェクト直下に置いた自分のPDFが、配布zipのソースに入るところだった。PDFは入れない。"""
    _fake_tree(tmp_path)
    (tmp_path / "my_document.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "app" / "inner.PDF").write_bytes(b"%PDF-1.4")
    names = [p.as_posix() for p in bp.source_files(tmp_path)]
    assert not [n for n in names if n.lower().endswith(".pdf")]


def test_real_project_source_zip_has_no_models_or_secrets(tmp_path):
    z = bp.make_source_zip(ROOT, tmp_path / "src.zip")
    with zipfile.ZipFile(z) as f:
        names = f.namelist()
    assert any(n.endswith("app/summary/fidelity.py") for n in names) and any(n.endswith("/LICENSE") for n in names)
    assert not [n for n in names if n.lower().endswith((".gguf", ".pyc", ".csv", ".pdf")) or "/.venv/" in n or "/samples/" in n or "/build/" in n]


def test_stage_static_and_zip_layout(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "LocalDocAI.exe").write_text("exe")
    bp.stage_static(stage, with_model=False)
    for name in ("使い方.txt", "get_model.bat", "get_model.ps1", "check.bat", "LICENSE", "THIRD_PARTY_NOTICES.txt", "VERSION.txt"):
        assert (stage / name).is_file(), name
    assert any((stage / "models").glob("*.txt"))                                    # モデルを置く場所の案内
    z = bp.make_zip(stage, tmp_path / "out.zip")
    with zipfile.ZipFile(z) as f:
        names = f.namelist()
    assert all(n.startswith("LocalDocAI/") for n in names)                          # 展開すると1つのフォルダになる
    assert "LocalDocAI/使い方.txt" in names and "LocalDocAI/LocalDocAI.exe" in names
    assert names == sorted(names)                                                   # 並びが固定（同じ入力なら同じ内容）


def test_with_model_package_has_no_placeholder_note(tmp_path):
    stage = tmp_path / "s"
    stage.mkdir()
    bp.stage_static(stage, with_model=True)
    assert not any((stage / "models").glob("*.txt"))
    assert "model: bundled" in (stage / "VERSION.txt").read_text(encoding="utf-8")


def test_zip_name_and_digest(tmp_path):
    assert bp.zip_name(with_model=False).startswith(f"LocalDocAI-{config.APP_VERSION}-")
    assert bp.zip_name(with_model=True).endswith("-with-model.zip")
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    assert bp.sha256_of(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_collect_licenses_copies_texts_and_static_files(tmp_path):
    missing = bp.collect_licenses(tmp_path / "lic")
    got = {p.name for p in (tmp_path / "lic").iterdir()}
    assert {"LGPL-3.0.txt", "Apache-2.0.txt", "MIT-llama.cpp.txt", "PyMuPDF-COPYING", "psutil-LICENSE"} <= got
    assert "PyMuPDF" not in missing and "psutil" not in missing


def test_users_documents_are_not_part_of_any_package_script():
    """配布スクリプトが、文書を読み書きする処理を持たない（方針: 文書の内容は、利用者の「コピー」「保存」以外で書き出さない）。"""
    src = (ROOT / "tools/build_package.py").read_text(encoding="utf-8")
    assert "extract_pdf" not in src and "Summarizer" not in src


def test_guide_and_readme_say_the_output_is_not_guaranteed():
    """配布物と README に、内容の保証ができないこと・原文で確認することを書いておく。"""
    guide = (ROOT / "packaging/dist_files/使い方.txt").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in (guide, readme):
        assert "保証" in doc and "元のPDF" in doc and "AGPL-3.0" in doc
    assert "原文を読む負担を減らす" in readme and "原文を読む負担を減らす" in guide
