"""LocalDoc AI — Phase 1: コマンドライン版。

使い方:
    python main.py 資料.pdf                    # 高速モード（既定）
    python main.py 資料.pdf --mode detail      # 詳細モード
    python main.py 資料.pdf --extract-only     # LLMを使わず、抽出結果の統計だけ表示
    python main.py --check                     # 環境（モデル・llama-server）の確認

要約は標準出力、進行状況と計測は標準エラー出力に出す。
文書の内容はファイルに保存しない。保存したい場合は `> 結果.md` でリダイレクトする。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.cancel import CancelToken
from app.core.config import Config, default_threads, resolve_model, resolve_server
from app.core.errors import AssetNotFoundError, Cancelled, LocalDocError
from app.core.metrics import RunMetrics


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ローカルPDF要約（完全オフライン）")
    p.add_argument("pdf", nargs="?", help="要約するPDFファイル")
    p.add_argument("--mode", choices=["fast", "detail"], default="fast", help="fast=重要部分だけ高速 / detail=全文を階層的に要約")
    p.add_argument("--kind", choices=["auto", "paper", "general"], default="auto", help="資料の種類（auto=見出しから推定）")
    p.add_argument("--model", help="GGUFファイルのパス、または 1.7b / 4b（models フォルダ内を探す）")
    p.add_argument("--llama-server", help="llama-server の実行ファイルのパス")
    p.add_argument("--threads", type=int, help=f"生成用スレッド数（既定 {default_threads()}。要ベンチ）")
    p.add_argument("--threads-batch", type=int, help="入力処理用スレッド数（既定は --threads と同じ）")
    p.add_argument("--ctx", type=int, help="コンテキスト長（既定 8192）")
    p.add_argument("--time-budget", type=float, help="詳細モードの要約時間の目安（秒、既定300=5分、0で無制限）。超えそうなら重要部分から処理する")
    p.add_argument("--fast-tokens", type=int, help="高速モードで読ませる入力の量（トークン）。小さいほど速く、拾える内容は減る（既定 1500）")
    p.add_argument("--sources", action="store_true", help="根拠にした原文ブロックも表示する")
    p.add_argument("--json", action="store_true", help="結果をJSONで出力する")
    p.add_argument("--extract-only", action="store_true", help="PDFの抽出と整形だけを行い、統計を表示する")
    p.add_argument("--raw", action="store_true", help="診断用: モデルの生の出力を標準エラー出力に表示する（文書の内容が画面に出る）")
    p.add_argument("--check", action="store_true", help="環境の確認だけを行う")
    return p


def make_config(args: argparse.Namespace) -> Config:
    return Config().with_overrides(
        threads=args.threads,
        threads_batch=args.threads_batch,
        ctx_size=args.ctx,
        time_budget_s=args.time_budget,
        fast_input_tokens=args.fast_tokens,
    )


def cmd_check(args: argparse.Namespace) -> int:
    cfg = make_config(args)
    ok = True
    log(f"Python          : {sys.version.split()[0]}")
    try:
        import pymupdf
        log(f"PyMuPDF         : {pymupdf.VersionBind}")
    except ImportError:
        log("PyMuPDF         : 未インストール（pip install -r requirements.txt）")
        ok = False
    try:
        import psutil  # noqa: F401
        log("psutil          : OK（RAM計測が使えます）")
    except ImportError:
        log("psutil          : 未インストール（RAM計測は「計測不可」になります）")
    try:
        exe = resolve_server(cfg, args.llama_server)
        ver = subprocess.run([str(exe), "--version"], capture_output=True, timeout=30, cwd=str(exe.parent))
        first = (ver.stdout + ver.stderr).decode("utf-8", errors="replace").strip().splitlines()
        log(f"llama-server    : {exe}")
        log(f"  version       : {first[0] if first else '不明'}")
        from app.llm.engine import REQUIRED_FLAGS, detect_flags
        flags = detect_flags(exe)
        missing = [f for f in REQUIRED_FLAGS if f not in flags]
        log("  必須引数      : " + ("OK" if not missing else "不足 " + ", ".join(missing)))
        ok &= not missing
    except (AssetNotFoundError, OSError, subprocess.SubprocessError) as e:
        log(f"llama-server    : {e}")
        ok = False
    try:
        model = resolve_model(cfg, args.model)
        log(f"モデル          : {model.name}（{model.stat().st_size / 1e9:.2f} GB）")
    except AssetNotFoundError as e:
        log(f"モデル          : {e}")
        ok = False
    log(f"既定スレッド数  : {cfg.threads}（論理コア {os.cpu_count()}）")
    log("結果: " + ("実行できる状態です" if ok else "不足があります。上の表示を確認してください"))
    return 0 if ok else 1


def cmd_extract_only(args: argparse.Namespace) -> int:
    from app.pipeline import prepare
    from app.pdf.text_processor import estimate_tokens

    cfg = make_config(args)
    m = RunMetrics()
    prep = prepare(args.pdf, cfg, m)
    blocks = prep.cleaned.blocks
    tokens = sum(estimate_tokens(b.text) for b in blocks)
    log(f"ページ数        : {prep.extraction.page_count}")
    log(f"抽出ブロック    : {len(prep.extraction.blocks)} → 整形後 {len(blocks)}（見出し {sum(b.is_heading for b in blocks)}）")
    log(f"文字数          : {prep.extraction.total_chars:,}（整形後 {sum(len(b.text) for b in blocks):,}）")
    log(f"推定トークン数  : 約 {tokens:,}（保守的な見積り）")
    log(f"資料の種類      : {prep.cleaned.doc_kind}")
    log(f"読み込み時間    : {m.pdf_load_s:.2f} 秒")
    for w in prep.warnings:
        log(f"注意: {w}")
    return 0


def run_cancellable(fn, cancel: CancelToken):
    """fn を別スレッドで実行し、Ctrl+C で中止できるようにする（Windows でも応答させるため）。"""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.2)
    except KeyboardInterrupt:
        log("\n中止しています…")
        cancel.set()
        th.join(10)
        raise Cancelled("処理が中止されました。") from None
    if "error" in box:
        raise box["error"]
    return box["value"]


def cmd_summarize(args: argparse.Namespace) -> int:
    from app.llm.engine import LlamaServerEngine
    from app.pipeline import prepare, summarize

    cfg = make_config(args)
    metrics = RunMetrics()
    cancel = CancelToken()
    t_all = time.perf_counter()

    log("[1/3] PDFを読み込んでいます…")
    prep = prepare(args.pdf, cfg, metrics)
    log(f"      {prep.extraction.page_count}ページ、{len(prep.cleaned.blocks)}ブロック（{metrics.pdf_load_s:.2f}秒）")

    model = resolve_model(cfg, args.model)
    server = resolve_server(cfg, args.llama_server)
    log(f"[2/3] モデルを起動しています…（{model.name}）")
    with LlamaServerEngine(cfg, model, server) as engine:
        metrics.model_start_s = engine.startup_s
        log(f"      起動完了（{metrics.model_start_s:.1f}秒）")

        def progress(stage: str, done: int, total: int) -> None:
            log(f"[3/3] 要約中: {stage} {done}/{total}")

        def raw_hook(label: str, text: str) -> None:
            log(f"\n----- モデルの生の出力 [{label}] -----\n{text}\n----- ここまで -----")

        result = run_cancellable(
            lambda: summarize(prep, cfg, engine, mode=args.mode, kind=args.kind,
                              metrics=metrics, progress=progress, cancel=cancel,
                              raw_hook=raw_hook if args.raw else None),
            cancel,
        )
        metrics.peak_rss_mb = engine.peak_rss_mb

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.render_text(show_sources=args.sources))
    log("")
    log("---- 計測（この実行の実測値）----")
    for line in metrics.report_lines():
        log(line)
    log(f"全体の所要時間  : {time.perf_counter() - t_all:.1f} 秒（PDF読み込み・モデル起動を含む）")
    return 0


def main(argv: list[str] | None = None) -> int:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if args.check:
            return cmd_check(args)
        if not args.pdf:
            build_parser().print_help(sys.stderr)
            return 2
        if args.extract_only:
            return cmd_extract_only(args)
        return cmd_summarize(args)
    except (Cancelled, KeyboardInterrupt) as e:
        log(str(e) if isinstance(e, Cancelled) else "\n中止しました。")
        return 130
    except LocalDocError as e:
        log(f"エラー: {e}")
        return 1
    except ValueError as e:
        log(f"設定エラー: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
