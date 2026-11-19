"""配布用の zip を作る（Windows 11 向け。PyInstaller の onedir）。

    python tools/build_package.py                       # llama-server は同梱、モデルは同梱しない（zip は約100〜200MB）
    python tools/build_package.py --with-model          # モデル（Qwen3-1.7B Q8_0）も同梱（約2GB）。そのまま完全オフラインで動く
    python tools/build_package.py --skip-llama          # llama-server を同梱しない（開発・確認用）
    python tools/build_package.py --llama-dir C:\\x\\llama --model-file C:\\x\\Qwen3-1.7B-Q8_0.gguf   # 取得済みのものを使う

できるもの（build フォルダ）:
    LocalDocAI-<版>-win64[-with-model].zip   展開すると LocalDocAI フォルダ（LocalDocAI.exe, llama/, models/, licenses/, source/ ...）
    同名の .sha256                           配布するときの改ざん確認用ハッシュ

■ ネット接続を使うのは、llama-server（GitHub）とモデル（Hugging Face）の取得のときだけです。
■ PyInstaller は、作る OS の実行ファイルしか作れません。Windows 用の zip は、Windows 上でこのスクリプトを実行してください。
   Linux 上で実行すると、動作確認用の Linux 版 zip（-linux-test）ができます（配布用ではありません）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.core.config import APP_VERSION, LLAMA_CPP_TAG  # noqa: E402

PKG = ROOT / "packaging"
APP_NAME = "LocalDocAI"
IS_WIN = sys.platform == "win32"
EXE_NAME = f"{APP_NAME}.exe" if IS_WIN else APP_NAME

# ソースコードのzipに入れないもの（モデル・ビルド成果物・自分の資料・仮想環境）
SOURCE_EXCLUDE_DIRS = {".venv", "venv", "env", "build", "dist", "__pycache__", ".pytest_cache", ".git", ".idea", ".vscode",
                       "models", "llama", "samples", ".mypy_cache", ".ruff_cache"}
SOURCE_EXCLUDE_SUFFIXES = {".gguf", ".part", ".pyc", ".pyo", ".csv", ".zip", ".log", ".pdf"}   # .pdf: 要約に使った自分の資料を、配布物に紛れ込ませない

# 同梱するライブラリのライセンス全文を、インストール済みの配布物から集める（ディストリビューション名 → 保存名）
LICENSE_DISTS = {
    "PyMuPDF": "PyMuPDF-COPYING",
    "psutil": "psutil-LICENSE",
    "pyinstaller": "PyInstaller-COPYING.txt",
}


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- 純粋な部品（テストできる）

def source_files(root: Path) -> list[Path]:
    """ソースコードの zip に入れるファイル（相対パスの並び。順序は固定）。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SOURCE_EXCLUDE_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() in SOURCE_EXCLUDE_SUFFIXES:
                continue
            out.append(p.relative_to(root))
    return out


def make_source_zip(root: Path, dest: Path) -> Path:
    """AGPL-3.0 のための、対応するソースコード一式。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in source_files(root):
            z.write(root / rel, f"LocalDocAI-source/{rel.as_posix()}")
    return dest


def collect_licenses(dest: Path) -> list[str]:
    """インストール済みの配布物から、ライセンス全文をコピーする。見つからないものは名前を返す（警告用）。"""
    dest.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for dist_name, out_name in LICENSE_DISTS.items():
        try:
            dist = md.distribution(dist_name)
        except md.PackageNotFoundError:
            missing.append(dist_name)
            continue
        cands = [f for f in (dist.files or []) if re.search(r"(licen[sc]e|copying)", f.name, re.I)]
        if not cands:
            missing.append(dist_name)
            continue
        shutil.copyfile(dist.locate_file(cands[0]), dest / out_name)
    psf = Path(sys.base_prefix) / "LICENSE.txt"                 # Windows の python.org 版は、インストール先に置かれている
    if psf.is_file():
        shutil.copyfile(psf, dest / "PSF-Python.txt")
    else:
        missing.append("Python(PSF)")
    for f in sorted((PKG / "licenses").glob("*")):
        shutil.copyfile(f, dest / f.name)
    return missing


def stage_static(stage: Path, *, with_model: bool) -> None:
    """exe の隣に置く、説明・ライセンス・補助スクリプト。"""
    for f in sorted((PKG / "dist_files").glob("*")):
        shutil.copyfile(f, stage / f.name)
    shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
    shutil.copyfile(PKG / "THIRD_PARTY_NOTICES.txt", stage / "THIRD_PARTY_NOTICES.txt")
    (stage / "models").mkdir(exist_ok=True)
    if not with_model:
        (stage / "models" / "ここにモデル（.gguf）を置きます.txt").write_text(
            "get_model.bat を実行すると、モデルがこのフォルダにダウンロードされます。\n", encoding="utf-8")
    (stage / "VERSION.txt").write_text(
        f"LocalDoc AI {APP_VERSION}\nllama.cpp {LLAMA_CPP_TAG}\n" + ("model: bundled\n" if with_model else "model: not bundled\n"),
        encoding="utf-8")


def zip_name(*, with_model: bool) -> str:
    plat = "win64" if IS_WIN else "linux-test"
    return f"{APP_NAME}-{APP_VERSION}-{plat}{'-with-model' if with_model else ''}.zip"


def make_zip(stage: Path, dest: Path) -> Path:
    """stage フォルダを、先頭が LocalDocAI/ の zip にする（日本語のファイル名は UTF-8 で保存される）。"""
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, f"{APP_NAME}/{p.relative_to(stage).as_posix()}")
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 手順

def run_pyinstaller(work: Path, dist: Path) -> Path:
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError as e:
            raise BuildError("PyInstaller が入っていません。`pip install -r requirements-build.txt` を実行してください。") from e
    for d in (work, dist):
        shutil.rmtree(d, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", str(PKG / f"{APP_NAME}.spec"), "--noconfirm", "--clean",
           "--workpath", str(work), "--distpath", str(dist)]
    print("PyInstaller を実行します…（数分かかります）")
    r = subprocess.run(cmd, cwd=ROOT)
    out = dist / APP_NAME
    if r.returncode != 0 or not (out / EXE_NAME).is_file():
        raise BuildError("PyInstaller が失敗しました。上のメッセージを確認してください。")
    return out


def selftest(stage: Path) -> bool:
    """できた実行ファイルで --selftest を走らせる（画面は出さない。文書は読まない）。"""
    report = stage.parent / "selftest.txt"
    report.unlink(missing_ok=True)
    env = dict(os.environ)
    if not IS_WIN:
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        r = subprocess.run([str(stage / EXE_NAME), "--selftest", str(report)], env=env, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  自己診断を実行できませんでした: {e}")
        return False
    text = report.read_text(encoding="utf-8") if report.is_file() else "(レポートなし)"
    print(text)
    return r.returncode == 0 and "RESULT OK" in text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LocalDoc AI の配布zipを作る")
    ap.add_argument("--with-model", action="store_true", help="モデル（Qwen3-1.7B Q8_0）も同梱する（約2GB）")
    ap.add_argument("--model-file", type=Path, help="取得済みの .gguf を使う（--with-model と併用）")
    ap.add_argument("--skip-llama", action="store_true", help="llama-server を同梱しない（開発・確認用）")
    ap.add_argument("--llama-dir", type=Path, help="取得済みの llama-server のフォルダを使う")
    ap.add_argument("--out", type=Path, default=ROOT / "build", help="出力先（既定: build）")
    ap.add_argument("--skip-selftest", action="store_true")
    args = ap.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    stage = run_pyinstaller(out / "work", out / "dist")

    # llama-server
    if not args.skip_llama:
        dest = stage / "llama"
        if args.llama_dir:
            if not any(args.llama_dir.rglob("llama-server*")):
                raise BuildError(f"{args.llama_dir} に llama-server が見つかりません。")
            shutil.copytree(args.llama_dir, dest, dirs_exist_ok=True)
        else:
            import setup_assets
            dest.mkdir(exist_ok=True)
            setup_assets.setup_llama(LLAMA_CPP_TAG, dest)
        if not any(dest.rglob("llama-server*")):
            raise BuildError("llama フォルダに llama-server がありません。")
    else:
        print("※ llama-server を同梱しません（--skip-llama）。")

    # モデル
    stage_static(stage, with_model=args.with_model)
    if args.with_model:
        if args.model_file:
            shutil.copyfile(args.model_file, stage / "models" / args.model_file.name)
        else:
            import setup_assets
            repo, quant = setup_assets.PRESETS["1.7b"]
            setup_assets.setup_model(repo, quant, stage / "models")

    # ライセンスとソースコード
    missing = collect_licenses(stage / "licenses")
    if missing:
        print("※ 次のライセンス全文を集められませんでした（配布前に licenses フォルダへ手で追加してください）: " + ", ".join(missing))
    make_source_zip(ROOT, stage / "source" / "LocalDocAI-source.zip")

    ok = True if args.skip_selftest else selftest(stage)
    if not ok:
        raise BuildError("自己診断（--selftest）に失敗しました。zip は作りません。")

    zpath = make_zip(stage, out / zip_name(with_model=args.with_model))
    digest = sha256_of(zpath)
    (zpath.parent / (zpath.name + ".sha256")).write_text(f"{digest} *{zpath.name}\n", encoding="utf-8")
    print(f"\nできました: {zpath}（{zpath.stat().st_size / 1e6:.0f} MB）\nSHA-256: {digest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"エラー: {e}")
        sys.exit(1)
