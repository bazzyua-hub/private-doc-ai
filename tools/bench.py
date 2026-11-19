"""対象PCでの速度計測（企画書 8.1 / 8.3）。スレッド数などの組み合わせを振って実測する。

    python tools/bench.py 資料.pdf --mode fast --grid 4,6,8,12 --runs 2
    python tools/bench.py 資料.pdf --mode detail --grid 8,8:12 --model 4b --csv bench.csv

--grid     : 生成スレッド数 t、または t:tb（tb=入力処理用スレッド数）のカンマ区切り。P/Eコア混在CPUでは全論理コアより少ない方が速いことがある。
--runs     : 同じサーバーで繰り返す回数。1回目（初回）と2回目以降を分けて表示する。
--cooldown : 組み合わせごとの休止秒数。薄型ノートの熱による速度低下を避けたいときに使う。
--cache    : プロンプトキャッシュを有効にする。既定は無効（同じPDFを繰り返すと2回目以降は入力処理が省略され、
             実際の使い方より極端に速く見えるため）。

結果には文書の内容を含めない（時間・トークン数・RAM のみ）。連続実行時の速度低下も見たいので、--runs 3 以上も試すこと。
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Config, resolve_model, resolve_server  # noqa: E402
from app.core.errors import LocalDocError  # noqa: E402
from app.core.metrics import RunMetrics  # noqa: E402
from app.llm.engine import LlamaServerEngine  # noqa: E402
from app.pipeline import prepare, summarize  # noqa: E402

HEADER = ["threads", "batch_thr", "run", "start_s", "total_s", "calls", "prompt_tps", "gen_tps", "peak_rss_mb", "unverified_numbers"]


def parse_grid(text: str) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        t, _, tb = item.partition(":")
        out.append((int(t), int(tb) if tb else None))
    return out


def fmt(v, digits=1):
    return "-" if v is None else f"{v:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf")
    p.add_argument("--mode", choices=["fast", "detail"], default="fast")
    p.add_argument("--grid", default="4,8", help="例: 4,6,8,12 または 8,8:12")
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--model")
    p.add_argument("--llama-server")
    p.add_argument("--ctx", type=int)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--cache", action="store_true", help="プロンプトキャッシュを有効にして測る（既定は無効）")
    p.add_argument("--csv", help="結果をCSVにも書き出す（内容は含まない）")
    args = p.parse_args(argv)

    base = Config().with_overrides(ctx_size=args.ctx, cache_prompt=args.cache)
    try:
        model, server = resolve_model(base, args.model), resolve_server(base, args.llama_server)
        prep = prepare(args.pdf, base)
        print(f"PDF: {prep.extraction.page_count}ページ / 整形後 {len(prep.cleaned.blocks)}ブロック（読み込み {prep.extraction.elapsed_s:.2f}秒）")
        print(f"モデル: {model.name} / モード: {args.mode}\n")
        rows: list[list] = []
        print("  ".join(f"{h:>11}" for h in HEADER))
        for gi, (t, tb) in enumerate(parse_grid(args.grid)):
            if gi and args.cooldown:
                time.sleep(args.cooldown)
            cfg = base.with_overrides(threads=t, threads_batch=tb)
            with LlamaServerEngine(cfg, model, server) as eng:
                for run in range(1, args.runs + 1):
                    m = RunMetrics()
                    res = summarize(prep, cfg, eng, mode=args.mode, metrics=m)
                    unverified = sum(len(c.unverified_numbers) for s in res.sections for c in s.claims)
                    row = [t, tb or t, run, eng.startup_s if run == 1 else None, m.summarize_s, len(m.calls),
                           m.prompt_tps, m.gen_tps, eng.peak_rss_mb, unverified]
                    rows.append(row)
                    cells = [f"{row[0]:>11}", f"{row[1]:>11}", f"{row[2]:>11}", f"{fmt(row[3]):>11}", f"{fmt(row[4]):>11}",
                             f"{row[5]:>11}", f"{fmt(row[6]):>11}", f"{fmt(row[7]):>11}", f"{fmt(row[8], 0):>11}", f"{row[9]:>11}"]
                    print("  ".join(cells), flush=True)
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(HEADER)
                w.writerows(rows)
            print(f"\nCSV を書き出しました: {args.csv}")
        print("\nstart_s はモデル起動（run 1 のみ）。total_s は要約処理のみで、起動時間は含みません。")
    except LocalDocError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
