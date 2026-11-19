"""要約の簡易評価: 仕込んだ事実がどれだけ残ったか、根拠にない数値がどれだけ出たかを機械的に数える。

    python tools/make_sample_pdfs.py
    python tools/eval_facts.py samples/lecture_20p.pdf --mode detail --model 1.7b

- 「保持された事実」: facts.json の needle（数値など）が要約テキストに含まれる割合。
  高速モードは重要部分の抜粋なので、100% にならないのは仕様。モデル・量子化・モードの比較に使う。
- 「根拠なし数値」: 要約中の数値のうち、引用ページに見当たらないもの（別のページには有る場合の警告。資料のどこにも無い数値は、
  その記述ごと表示されない）。
- 「意味のずれで原文に置換」: 数値の対象・因果・条件・限定などが原文とずれる恐れがあり、原文の文に置き換えた記述の数。
- 「確認できず非表示」: 原文との照合で確認できなかったため、表示しなかった記述の数（モデルが原文に無い内容を書いた目安）。
- 「作業を求める文」: 資料にある、読者への明示的な作業指示（課題カード＋文）の数。モデルは書かない。
- 「概要の長さ」「主要ポイント数」: 情報量の目安（概要は200〜350字程度、主要ポイントは5〜8項目程度が目標）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Config, resolve_model, resolve_server  # noqa: E402
from app.core.errors import LocalDocError  # noqa: E402
from app.core.metrics import RunMetrics  # noqa: E402
from app.llm.engine import LlamaServerEngine  # noqa: E402
from app.pipeline import prepare, summarize  # noqa: E402
from app.summary.schemas import STATUS_UNRESOLVED  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf")
    p.add_argument("--mode", choices=["fast", "detail"], default="fast")
    p.add_argument("--facts", default=None, help="facts.json（既定: PDFと同じフォルダ）")
    p.add_argument("--model")
    p.add_argument("--llama-server")
    p.add_argument("--threads", type=int)
    args = p.parse_args()

    pdf = Path(args.pdf)
    facts_path = Path(args.facts) if args.facts else pdf.parent / "facts.json"
    try:
        facts = [f for f in json.loads(facts_path.read_text(encoding="utf-8")) if f["pdf"] == pdf.name]
        if not facts:
            print(f"{facts_path} に {pdf.name} の事実が登録されていません。", file=sys.stderr)
            return 1
        cfg = Config().with_overrides(threads=args.threads)
        prep = prepare(pdf, cfg)
        with LlamaServerEngine(cfg, resolve_model(cfg, args.model), resolve_server(cfg, args.llama_server)) as eng:
            m = RunMetrics()
            res = summarize(prep, cfg, eng, mode=args.mode, metrics=m)
    except (LocalDocError, OSError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    text = res.render_text()
    kept = [f for f in facts if f["needle"] in text]
    claims = [c for s in res.sections for c in s.claims]
    bad_nums = sum(len(c.unverified_numbers) for c in claims)
    unresolved = sum(c.status == STATUS_UNRESOLVED for c in claims)
    print(f"モード {args.mode} / {len(prep.cleaned.blocks)}ブロック / 要約処理 {m.summarize_s:.1f}秒")
    print(f"保持された事実 : {len(kept)}/{len(facts)}（{100 * len(kept) / len(facts):.0f}%）")
    print(f"根拠なし数値   : {bad_nums} 件（主張 {len(claims)} 件中）")
    print(f"意味のずれで原文に置換: {res.fidelity_replaced} 件（数値が指す対象・因果・条件・限定・否定などが原文とずれる恐れがあったもの）")
    print(f"確認できず非表示: {res.unverified_dropped} 件（原文との照合で、確認できなかった記述）")
    print(f"参照箇所未特定 : {unresolved} 件（表示された主張のうち）")
    overview = "".join(c.text for sec in res.sections if sec.title == "概要" for c in sec.claims)
    points = sum(len(sec.claims) for sec in res.sections if sec.title == "主要ポイント")
    tasks = sum(len(t.do) + len(t.steps) for t in res.tasks) + len(res.instructions)
    print(f"概要の長さ     : {len(overview)} 字 / 主要ポイント {points} 項目 / 作業を求める文・課題 {tasks} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
