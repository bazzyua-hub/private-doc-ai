"""ベンチ・評価用のダミーPDFを作る（機密資料を使わずに速度と品質を比べるため）。

    python tools/make_sample_pdfs.py            # samples/ に作成
    python tools/make_sample_pdfs.py --out 別のフォルダ

作るもの:
    lecture_20p.pdf  日本語の講義スライド風 20ページ（見出し + 箇条書き。ヘッダー/フッター付き）
    lecture_40p.pdf  同 40ページ
    paper_en.pdf     英語の論文風（Abstract/Introduction/Methods/Results/Conclusion/References、2段組）
    facts.json       各PDFに仕込んだ「要約に残ってほしい事実」（数値つき）。eval_facts.py が使う

内容はすべて架空です（数値・条件は検証用に仕込んだもの）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

TOPICS = [
    ("正規化", "第三正規形まで進めると更新時の不整合を防げる", "冗長なデータが約{a}%減少した", "過度な正規化は結合の増加で検索が遅くなる"),
    ("インデックス", "B木インデックスは範囲検索に強い", "検索時間が{a}倍高速化した", "更新が多い表では書き込みが約{b}%遅くなる"),
    ("トランザクション", "ACID特性が整合性を支える", "ロック待ちの上限は{a}秒に設定する", "分離レベルを上げるほど同時実行性は下がる"),
    ("バックアップ", "全体バックアップと差分バックアップを併用する", "復旧目標時間（RTO）は{a}分以内とする", "検証していないバックアップは復旧できる保証がない"),
    ("SQL最適化", "実行計画を確認してから書き換える", "処理時間が{a}%短縮された", "統計情報が古いと誤った計画が選ばれる"),
    ("分散システム", "CAP定理で一貫性と可用性のトレードオフを説明する", "レプリカ数は{a}台を標準とする", "ネットワーク分断時は一貫性か可用性のどちらかを選ぶ"),
    ("セキュリティ", "最小権限の原則でアカウントを管理する", "パスワードの最小長は{a}文字とする", "暗号化しても鍵の管理が甘ければ意味がない"),
    ("監視", "遅いクエリのログを定期的に確認する", "しきい値は{a}ミリ秒に設定した", "閾値が低すぎるとログが膨大になる"),
]
EN_PARAS = {
    "Abstract": "We propose a lightweight method for summarising long documents on consumer hardware. Our approach reduces latency by {a}% compared with the baseline while preserving key figures.",
    "Introduction": "Large language models are usually served from the cloud, which is unsuitable for confidential documents. We study on-device summarisation using quantised models with {b} billion parameters.",
    "Methods": "We split each document into chunks of about {c} tokens, summarise each chunk, and merge the partial summaries. All experiments ran on a laptop CPU without a GPU.",
    "Results": "The proposed method achieved a ROUGE-L score of {d} and reduced memory use by {a}%. Runtime scaled linearly with document length up to 40 pages.",
    "Conclusion": "On-device summarisation is practical for short documents. A limitation is that figures embedded as images are not read, so results may omit some content.",
    "References": "[1] A. Author. A Fictional Reference. 2020. [2] B. Writer. Another Fictional Reference. 2021.",
}


def lecture(path: Path, n_pages: int) -> list[dict]:
    doc = pymupdf.open()
    facts = []
    for n in range(1, n_pages + 1):
        title, point, metric, caveat = TOPICS[(n - 1) % len(TOPICS)]
        a, b = 10 + (n * 7) % 80 + 0.5, 5 + (n * 3) % 20
        metric = metric.format(a=a, b=b)
        caveat = caveat.format(a=a, b=b)
        page = doc.new_page(width=720, height=405)             # 16:9 スライド
        page.insert_text((30, 22), "データベース特論 第5回 2026年度", fontname="japan", fontsize=8)
        page.insert_text((40, 80), f"{n}. {title}", fontname="japan", fontsize=26)
        y = 130
        for line in (f"・{point}", f"・{metric}", f"・{caveat}"):
            page.insert_text((60, y), line, fontname="japan", fontsize=15)
            y += 50
        page.insert_text((350, 390), f"{n}", fontname="helv", fontsize=9)
        facts.append({"pdf": path.name, "page": n, "needle": f"{a}"})
    doc.save(str(path))
    doc.close()
    return facts


def paper(path: Path) -> list[dict]:
    vals = dict(a=37.5, b=1.7, c=1500, d=0.42)
    doc = pymupdf.open()
    facts = []
    page = doc.new_page()
    page.insert_text((50, 60), "On-device Summarisation of Long Documents", fontname="helv", fontsize=18)
    left, right = ["Abstract", "Introduction", "Methods"], ["Results", "Conclusion", "References"]
    for col, (names, x0, x1) in enumerate(((left, 50, 290), (right, 310, 545))):
        y = 110
        for name in names:
            page.insert_text((x0, y), name, fontname="helv", fontsize=13)
            page.insert_textbox(pymupdf.Rect(x0, y + 8, x1, y + 190), EN_PARAS[name].format(**vals), fontname="helv", fontsize=10)
            y += 200
    for needle in ("37.5", "1.7 billion", "1500", "0.42"):
        facts.append({"pdf": path.name, "page": 1, "needle": needle})
    doc.save(str(path))
    doc.close()
    return facts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "samples"))
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    facts = lecture(out / "lecture_20p.pdf", 20) + lecture(out / "lecture_40p.pdf", 40) + paper(out / "paper_en.pdf")
    (out / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"作成しました: {out}")
    for f in sorted(out.iterdir()):
        print(f"  {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
