"""要約の1文に対する、根拠ブロックの推定（モデルが参照番号を付けなかった場合の補助）。

小型モデルは、統合の段階で参照番号を付けないことが実測で確認された。そこでモデルには番号を
「思い出させず」、プログラムが文字2-gramの重なりで原文ブロックを探す。

- 稀な2-gramほど重く数える（「する」「ある」のような頻出語に引きずられない）
- 1文が複数ブロックにまたがることが多いので、被覆が増える限り最大4ブロックまで貪欲に選ぶ
- 文の2-gram（重みつき）の4割以上を説明できなければ「特定できない」とする（原文に無い内容を作った文を弾く）

結果は推定であり、利用者には「本文との照合による推定」と明示して表示する。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from typing import Iterable

from app.pdf.extractor import Block


# 専門用語・固有名詞に当たる語（3文字以上の漢字の連続・カタカナ・英数字）。言い換えで作られにくく、作り話で現れやすい
_TERM = re.compile(r"[一-鿿々]{3,}|[ァ-ヶー]{3,}|[A-Za-z][A-Za-z0-9\-]{2,}")


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())
    return {t[i:i + 2] for i in range(len(t) - 1)}


class Attributor:
    def __init__(self, blocks: Iterable[Block]) -> None:
        self._index: dict[str, list[int]] = defaultdict(list)
        self._n = 0
        self._page: dict[int, int] = {}
        self._by_page: dict[int, set[int]] = defaultdict(set)
        for b in blocks:
            self._n += 1
            self._page[b.id] = b.page
            self._by_page[b.page].add(b.id)
            for bg in _bigrams(b.text):
                self._index[bg].append(b.id)

    def _weight(self, bg: str) -> float:
        df = len(self._index.get(bg, ()))
        return math.log(1.0 + self._n / (1.0 + df))

    def attribute(self, text: str, *, max_blocks: int = 4, min_cover: float = 0.4, min_gain: float = 0.12) -> list[int]:
        claim = _bigrams(text)
        if len(claim) < 6:
            return []
        weights = {bg: self._weight(bg) for bg in claim}
        total = sum(weights.values())
        remaining = set(claim)
        chosen: list[int] = []
        covered = 0.0
        for _ in range(max_blocks):
            gain: dict[int, float] = defaultdict(float)
            for bg in remaining:
                for bid in self._index.get(bg, ()):
                    gain[bid] += weights[bg]
            if not gain:
                break
            best = max(gain, key=lambda b: (gain[b], -b))
            if gain[best] < min_gain * total:
                break
            chosen.append(best)
            covered += gain[best]
            remaining = {bg for bg in remaining if best not in self._index.get(bg, ())}
        if not chosen or covered / total < min_cover:
            return []
        return sorted(chosen)

    def support(self, text: str, block_ids: Iterable[int]) -> float:
        """主張の2-gramのうち、引用された箇所（と同じページ）の本文で説明できる割合（0〜1、重みつき）。

        モデルが書いた参照番号が、実際にその文の根拠かどうかの検算に使う。同じページの隣のブロックを
        引用するずれは許容するため、ページ単位で見る。
        """
        claim = _bigrams(text)
        if len(claim) < 6:
            return 1.0                                  # 短すぎて判断できない文は疑わない
        pages = {self._page[i] for i in block_ids if i in self._page}
        ids = set().union(*(self._by_page[p] for p in pages)) if pages else set()
        total = covered = 0.0
        for bg in claim:
            w = self._weight(bg)
            total += w
            if ids.intersection(self._index.get(bg, ())):
                covered += w
        return covered / total if total else 1.0

    def coverage_in(self, text: str, sources: Iterable[str]) -> float:
        """文の2-gram（重みつき）のうち、与えた文章の集まりで説明できる割合（0〜1）。

        概要は、モデルに渡した要点の一覧からだけ作るべきものなので、一覧にない内容を足した文を見つける検算に使う。
        """
        claim = _bigrams(text)
        if len(claim) < 6:
            return 1.0
        have: set[str] = set()
        for t in sources:
            have |= _bigrams(t)
        total = sum(self._weight(bg) for bg in claim)
        covered = sum(self._weight(bg) for bg in claim if bg in have)
        return covered / total if total else 1.0

    def term_support(self, text: str, sources: Iterable[str], *, min_overlap: float = 0.6) -> tuple[float | None, list[str]]:
        """文中の専門用語・固有名詞のうち、与えた文章の集まりに見つかる割合と、見つからなかった語。

        見つかるとは、語の2-gramの6割以上が文章の2-gramにあること（「情報認知率」と「情報の認知率」のような
        言い換えは許す）。語が1つも無ければ (None, [])。
        """
        terms = _TERM.findall(text)
        if not terms:
            return None, []
        have: set[str] = set()
        for t in sources:
            have |= _bigrams(t)
        missing: list[str] = []
        for t in terms:
            bg = _bigrams(t)
            if bg and len(bg & have) / len(bg) < min_overlap:
                missing.append(t)
        return 1.0 - len(missing) / len(terms), missing
