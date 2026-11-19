"""要約結果のデータ構造と表示用の整形。GUI（Phase 3）も同じ構造を使う想定。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.metrics import RunMetrics
from app.pdf.extractor import Block

STATUS_OK = "ok"                  # 根拠ブロックを特定できた
STATUS_RANGE = "range_only"       # 該当範囲（ページ）までしか特定できない
STATUS_UNRESOLVED = "unresolved"  # 参照箇所未特定
STATUS_INFERRED = "inferred"      # モデルの引用ではなく、プログラムが本文との照合で推定した根拠
STATUS_OVERALL = "overall"        # 文書全体の要約（概要）。個別の参照は付かない


@dataclass
class Claim:
    text: str
    labels: list[str] = field(default_factory=list)       # 存在確認を通った参照ラベル（例: B12, S3）
    block_ids: list[int] = field(default_factory=list)    # プログラム側で解決した根拠ブロックID
    status: str = STATUS_OK
    unverified_numbers: list[str] = field(default_factory=list)  # 根拠に見当たらなかった数値


@dataclass
class TaskCard:
    """原文から抜き出した「やること」のまとまり（課題ごと、または番号つき手順ごと）。すべて原文のままの文。"""
    title: str                                   # "課題1" / "課題3a" / 手順の題
    topic: str = ""                              # 課題の題名（資料中にあれば）
    labeled: bool = True                         # 【課題N】の見出しがあったか
    place: str = ""                              # コマンドを実行する場所（例: ゲストOSコンソール/TeraTerm）
    do: list[Claim] = field(default_factory=list)            # やること（指示文）
    conditions: list[Claim] = field(default_factory=list)    # 条件
    steps: list[Claim] = field(default_factory=list)         # 手順（①②…）
    commands: list[Claim] = field(default_factory=list)      # コマンド例
    checks: list[Claim] = field(default_factory=list)        # 確認・提出

    def lines(self) -> list[tuple[str, list[Claim]]]:
        return [("やること", self.do), ("条件", self.conditions), ("手順", self.steps),
                ("コマンド", self.commands), ("確認・提出", self.checks)]

    def block_ids(self) -> list[int]:
        return sorted({i for _, cl in self.lines() for c in cl for i in c.block_ids})


@dataclass
class Section:
    title: str
    claims: list[Claim] = field(default_factory=list)


@dataclass
class SummaryResult:
    mode: str
    doc_kind: str
    source_name: str
    page_count: int
    sections: list[Section]
    warnings: list[str] = field(default_factory=list)
    evidence: dict[int, Block] = field(default_factory=dict)   # 引用された原文ブロック（メモリ上のみ）
    metrics: RunMetrics | None = None
    tasks: list[TaskCard] = field(default_factory=list)        # 原文から抜き出した課題・手順（モデルは使わない）
    task_outline: Claim | None = None                          # 全課題の一覧（資料に目次があれば）
    instructions: list[Claim] = field(default_factory=list)    # 課題カード以外の、作業を求める文（原文のまま1文ずつ）
    unverified_dropped: int = 0                                # 原文で確認できなかったため、表示しなかった記述の数
    fidelity_replaced: int = 0                                 # 意味がずれる恐れがあり、原文の文に置き換えた記述の数

    NO_TASKS = "該当なし（資料内に、読者への明示的な作業指示は見つかりませんでした）"

    # ------------------------------------------------------------ 表示

    def pages_of(self, block_ids: list[int]) -> list[int]:
        return sorted({self.evidence[i].page for i in block_ids if i in self.evidence})

    def render_text(self, *, show_sources: bool = False, snippet_chars: int = 160) -> str:
        out: list[str] = []
        mode_label = {"fast": "高速要約（重要部分の抽出）", "detail": "詳細要約"}.get(self.mode, self.mode)
        out.append(f"# {self.source_name}  —  {mode_label}（{self.page_count}ページ）")
        out.append("")
        for sec in self.sections:
            out.append(f"## {sec.title}")
            if not sec.claims:
                out.append("- （該当なし・モデルから有効な出力が得られませんでした）")
            for c in sec.claims:
                out.append(f"- {c.text}{self._suffix(c)}")
            out.append("")
        out.extend(self._render_tasks())
        if show_sources:
            snippets = self.evidence_snippets(snippet_chars)
            if snippets:
                out.append("## 参照した原文")
                out.extend(f"[B{i}] p.{page}: {text}" for i, page, text in snippets)
                out.append("")
        if self.warnings:
            out.append("## 処理上の注意")
            out.extend(f"- {w}" for w in self.warnings)
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    def cited_ids(self) -> list[int]:
        """表示した記述の根拠になっているブロックID（根拠を特定できたものだけ）。"""
        return sorted({i for sec in self.sections for c in sec.claims
                       if c.status in (STATUS_OK, STATUS_INFERRED) for i in c.block_ids}
                      | {i for t in self.tasks for i in t.block_ids()} | {i for c in self.instructions for i in c.block_ids})

    def evidence_snippets(self, max_chars: int = 160) -> list[tuple[int, int, str]]:
        """（ブロックID, ページ, 原文）の一覧。原文は抽出済みのテキストそのもので、モデルは関与しない。

        ブロックが長いときは、それを根拠にした記述と最も言葉の重なる1文を選んで見せる（先頭だけでは根拠が見えない）。
        """
        texts: dict[int, list[str]] = {}
        for sec in self.sections:
            for c in sec.claims:
                if c.status in (STATUS_OK, STATUS_INFERRED):
                    for i in c.block_ids:
                        texts.setdefault(i, []).append(c.text)
        out = []
        for i in self.cited_ids():
            b = self.evidence.get(i)
            if b is not None:
                out.append((i, b.page, pick_snippet(b.text, texts.get(i, []), max_chars)))
        return out

    def _render_tasks(self) -> list[str]:
        out = ["## やること・手順" + ("（原文から抜き出し）" if self.tasks or self.instructions else "")]
        if not self.tasks and not self.instructions:
            out.extend([f"- {self.NO_TASKS}", ""])
            return out
        if self.task_outline:
            out.append(f"- 全体: {self.task_outline.text}（{format_pages(self.pages_of(self.task_outline.block_ids))}）")
        out.append("")
        for card in self.tasks:
            pages = format_pages(self.pages_of(card.block_ids()))
            head = card.title + (f"：{card.topic}" if card.topic else "")
            out.append(f"### {head}（{pages}）" if pages else f"### {head}")
            multi_page = len(self.pages_of(card.block_ids())) > 1
            for label, claims in card.lines():
                if not claims:
                    continue
                def pg(c: Claim) -> str:
                    ps = self.pages_of(c.block_ids)
                    return f"（{format_pages(ps)}）" if multi_page and ps else ""
                if label == "コマンド":
                    out.append(f"- コマンド（{card.place}）:" if card.place else "- コマンド:")
                    out.extend(f"    {c.text}" for c in claims)
                elif label in ("手順", "条件") or len(claims) > 1:
                    out.append(f"- {label}:")
                    out.extend(f"    {'' if label == '手順' else '・'}{c.text}{pg(c)}" for c in claims)
                else:
                    out.append(f"- {label}: {claims[0].text}{pg(claims[0])}")
            out.append("")
        if self.instructions:
            out.append("### その他の、作業を求める文" if self.tasks else "### 作業を求める文")
            for c in self.instructions:
                out.append(f"- {c.text}（{format_pages(self.pages_of(c.block_ids))}）" if c.block_ids else f"- {c.text}")
            out.append("")
        return out

    def claim_notes(self, c: Claim) -> tuple[str, str]:
        """主張につける表記を（出典・状態の表記, 数値の警告）に分けて返す。GUIの色分け用。"""
        note = ""
        pages = self.pages_of(c.block_ids)
        if c.status == STATUS_UNRESOLVED:
            note = "（参照箇所未特定）"
        elif c.status == STATUS_INFERRED:
            note = f"（{format_pages(pages)}・本文との照合による推定）"
        elif c.status == STATUS_OVERALL:
            note = "（文書全体の要約）"
        elif c.status == STATUS_RANGE:
            note = f"（{format_pages(pages)} 付近・該当ブロックは未特定）"
        elif pages:
            note = f"（{format_pages(pages)}）"
        warn = f" ⚠数値が引用ページに見当たりません: {', '.join(c.unverified_numbers)}" if c.unverified_numbers else ""
        return note, warn

    def _suffix(self, c: Claim) -> str:
        return "".join(self.claim_notes(c))

    def to_dict(self) -> dict:
        return {
            "source": self.source_name,
            "pages": self.page_count,
            "mode": self.mode,
            "doc_kind": self.doc_kind,
            "sections": [
                {
                    "title": s.title,
                    "claims": [
                        {
                            "text": c.text,
                            "status": c.status,
                            "labels": c.labels,
                            "block_ids": c.block_ids,
                            "pages": self.pages_of(c.block_ids),
                            "unverified_numbers": c.unverified_numbers,
                        }
                        for c in s.claims
                    ],
                }
                for s in self.sections
            ],
            "tasks": [
                {
                    "title": t.title, "topic": t.topic, "labeled": t.labeled, "place": t.place,
                    **{name: [{"text": c.text, "block_ids": c.block_ids, "pages": self.pages_of(c.block_ids)} for c in cl]
                       for name, cl in (("do", t.do), ("conditions", t.conditions), ("steps", t.steps),
                                        ("commands", t.commands), ("checks", t.checks))},
                }
                for t in self.tasks
            ],
            "task_outline": self.task_outline.text if self.task_outline else "",
            "instructions": [{"text": c.text, "block_ids": c.block_ids, "pages": self.pages_of(c.block_ids)}
                             for c in self.instructions],
            "unverified_dropped": self.unverified_dropped,
            "fidelity_replaced": self.fidelity_replaced,
            "warnings": self.warnings,
            "metrics": self.metrics.to_dict() if self.metrics else None,
        }


def format_pages(pages: list[int]) -> str:
    """[3, 4, 5, 9] -> 'p.3-5, 9'"""
    if not pages:
        return ""
    runs: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    body = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
    return f"p.{body}"


_SENT_SPLIT = re.compile(r"(?<=[。．！？!?])\s*|\n+")


def pick_snippet(text: str, claim_texts: list[str], max_chars: int) -> str:
    """原文ブロックから、根拠として見せる部分を選ぶ。選ぶのは抽出済みの本文の一部であり、書き換えない。"""
    flat = re.sub(r"\s*\n\s*", " ", text).strip()
    if len(flat) <= max_chars:
        return flat
    from .attribution import _bigrams
    sents = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    want: set[str] = set()
    for t in claim_texts:
        want |= _bigrams(t)
    if want and len(sents) > 1:
        best = max(range(len(sents)), key=lambda k: (len(_bigrams(sents[k]) & want), -k))
        snippet = sents[best]
        # 短ければ、直前の文も添えて文脈を残す
        if best > 0 and len(snippet) + len(sents[best - 1]) + 1 <= max_chars:
            snippet = sents[best - 1] + " " + snippet
        lead = "…" if best > 0 and not snippet.startswith(sents[0]) else ""
        snippet = re.sub(r"\s*\n\s*", " ", snippet)
        return lead + (snippet if len(snippet) <= max_chars else snippet[:max_chars] + "…")
    return flat[:max_chars] + "…"
