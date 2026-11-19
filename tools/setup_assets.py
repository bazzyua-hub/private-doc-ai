"""初期セットアップ専用スクリプト: llama.cpp（Windows CPU版）とモデル(GGUF)をダウンロードする。

★ アプリの中でネットワークを使うのはこのスクリプトだけです（企画書 S-01: セットアップと通常処理の分離）。
   PDFの内容は一切送信されません。ダウンロードしたあとは、ネットワークを切っても要約できます。

使い方:
    python tools/setup_assets.py llama                 # 動作確認済みの版（config.LLAMA_CPP_TAG）を llama/ に展開
    python tools/setup_assets.py llama --tag latest    # 最新版（未検証。--check で必須引数を確認すること）
    python tools/setup_assets.py model 1.7b            # Qwen/Qwen3-1.7B-GGUF の Q8_0（公式にはこの量子化のみ）
    python tools/setup_assets.py model 4b              # Qwen/Qwen3-4B-GGUF の Q4_K_M
    python tools/setup_assets.py model --repo OWNER/REPO --quant Q4_K_M   # 任意のリポジトリ
    python tools/setup_assets.py all                   # llama と 1.7b と 4b をまとめて取得
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import APP_ROOT, LLAMA_CPP_TAG  # noqa: E402

GITHUB_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
HF = "https://huggingface.co"
PRESETS = {
    "1.7b": ("Qwen/Qwen3-1.7B-GGUF", "Q8_0"),
    "4b": ("Qwen/Qwen3-4B-GGUF", "Q4_K_M"),
}
LLAMA_ASSET = re.compile(r"^llama-b\d+-bin-win-cpu-x64\.zip$")
UA = {"User-Agent": "LocalDocAI-setup"}


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- 純粋関数（オフラインでテスト可能）

def pick_llama_asset(release: dict) -> dict | None:
    for a in release.get("assets", []):
        if LLAMA_ASSET.match(a.get("name", "")):
            return a
    return None


def pick_hf_file(siblings: list[dict], quant: str) -> dict:
    ggufs = [s for s in siblings if s.get("rfilename", "").lower().endswith(".gguf")]
    usable = [
        s for s in ggufs
        if quant.lower() in s["rfilename"].lower()
        and "mmproj" not in s["rfilename"].lower()
        and not re.search(r"-\d{5}-of-\d{5}", s["rfilename"])      # 分割ファイルは対象外
    ]
    if not usable:
        names = ", ".join(sorted(s["rfilename"] for s in ggufs)) or "（GGUFなし）"
        raise SetupError(f"量子化 '{quant}' のファイルが見つかりません。利用可能: {names}")
    exact = [s for s in usable if s["rfilename"].lower().endswith(f"-{quant.lower()}.gguf")]
    return (exact or usable)[0]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_extract(zip_path: Path, dest: Path) -> list[str]:
    """zip-slip（../ を含むパス）を拒否しながら展開する。"""
    dest = dest.resolve()
    names: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if dest != target and dest not in target.parents:
                raise SetupError(f"不正なパスを含むzipです: {info.filename}")
        z.extractall(dest)
        names = [i.filename for i in z.infolist() if not i.is_dir()]
    return names


# ---------------------------------------------------------------- ネットワーク

def http_json(url: str):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url: str, dest: Path, *, sha256: str | None = None, label: str = "") -> Path:
    """一時ファイル(.part)に保存し、ハッシュ検証に成功したら本来の名前に変更する。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256 and sha256_file(dest) == sha256.lower():
        print(f"  既に取得済み（ハッシュ一致）: {dest.name}")
        return dest
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        last = -1
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if pct != last and pct % 5 == 0:
                print(f"\r  {label or dest.name}: {done / 1e6:,.0f} / {total / 1e6:,.0f} MB ({pct}%)", end="", flush=True)
                last = pct
    print()
    if sha256 and h.hexdigest() != sha256.lower():
        part.unlink(missing_ok=True)
        raise SetupError(f"ハッシュが一致しません（ダウンロードが壊れた可能性）: {dest.name}")
    part.replace(dest)
    return dest


def setup_llama(tag: str, llama_dir: Path) -> None:
    print(f"llama.cpp を取得します（タグ: {tag}）")
    if tag == "latest":
        releases = http_json(f"{GITHUB_RELEASES}?per_page=20")
        release = next((r for r in releases if pick_llama_asset(r)), None)
        if release is None:
            raise SetupError("Windows CPU 版の配布ファイルを持つリリースが見つかりませんでした。")
    else:
        release = http_json(f"{GITHUB_RELEASES}/tags/{tag}")
    asset = pick_llama_asset(release)
    if asset is None:
        names = ", ".join(a["name"] for a in release.get("assets", [])[:20])
        raise SetupError(f"win-cpu-x64 の zip が見つかりません。配布ファイル: {names}")
    digest = (asset.get("digest") or "").removeprefix("sha256:") or None
    zpath = download(asset["browser_download_url"], llama_dir / asset["name"], sha256=digest)
    files = safe_extract(zpath, llama_dir)
    zpath.unlink(missing_ok=True)
    (llama_dir / "VERSION.txt").write_text(
        f"tag={release['tag_name']}\nasset={asset['name']}\nsha256={digest or '未検証'}\n", encoding="utf-8"
    )
    print(f"  展開しました: {llama_dir}（{len(files)}ファイル、版 {release['tag_name']}）")
    if tag == "latest":
        print("  ※ 最新版は動作未検証です。`python main.py --check` で必須引数が揃っていることを確認してください。")


def setup_model(repo: str, quant: str, models_dir: Path) -> None:
    print(f"モデルを取得します: {repo}（{quant}）")
    info = http_json(f"{HF}/api/models/{repo}?blobs=true")
    f = pick_hf_file(info.get("siblings", []), quant)
    sha = ((f.get("lfs") or {}).get("sha256")) or None
    url = f"{HF}/{repo}/resolve/main/{f['rfilename']}"
    size_gb = (f.get("size") or 0) / 1e9
    print(f"  ファイル: {f['rfilename']}（約 {size_gb:.2f} GB）")
    download(url, models_dir / Path(f["rfilename"]).name, sha256=sha)
    print(f"  保存しました: {models_dir / Path(f['rfilename']).name}" + ("（ハッシュ検証済み）" if sha else "（ハッシュ情報なし）"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="llama.cpp とモデルの取得（ネットワークを使うのはこのスクリプトのみ）")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("llama")
    pl.add_argument("--tag", default=LLAMA_CPP_TAG, help=f"リリースタグ（既定 {LLAMA_CPP_TAG}。latest で最新）")
    pm = sub.add_parser("model")
    pm.add_argument("preset", nargs="?", choices=sorted(PRESETS))
    pm.add_argument("--repo")
    pm.add_argument("--quant")
    sub.add_parser("all")
    args = p.parse_args(argv)

    print("【注意】このスクリプトはインターネットに接続します（初期セットアップ専用）。PDFの内容は送信されません。\n")
    try:
        if args.cmd == "llama":
            setup_llama(args.tag, APP_ROOT / "llama")
        elif args.cmd == "model":
            if args.preset:
                repo, quant = PRESETS[args.preset]
            elif args.repo and args.quant:
                repo, quant = args.repo, args.quant
            else:
                p.error("1.7b / 4b のどちらか、または --repo と --quant を指定してください")
            setup_model(repo, quant, APP_ROOT / "models")
        else:
            setup_llama(LLAMA_CPP_TAG, APP_ROOT / "llama")
            for repo, quant in PRESETS.values():
                setup_model(repo, quant, APP_ROOT / "models")
    except SetupError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"通信またはファイル操作に失敗しました: {e}", file=sys.stderr)
        return 1
    print("\n完了。次は `python main.py --check` で環境を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
