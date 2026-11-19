"""要約の1文が、原文の意味を保っているかの軽量な検査（モデルを呼ばない）。

attribution.py の照合は「言葉が原文にあるか」を見る。だが、言葉が全部あっても意味が変わることがある:
  原文: 大多数の態度が0.7〜0.8の間に収束した      要約: 態度と本心の分布が0.7〜0.8の範囲で収束
  → 数値は正しいが、数値が指す対象に「本心」「分布」が混ざっている。

ここでは、要約の1文と、その根拠の原文（のうち最も近い数文）を比べ、次の変化を検出する。
  1. 数値の対象のずれ   数値と同じ節にある語が、原文のその数値の節に無い（原文の別の箇所にある語を持ち込んだ）
  2. 因果・比較・条件   原文に無い「〜のため」「〜より」「〜の場合」を足した
  3. 限定の脱落         原文の「可能性がある」「とは限らない」「〜ではない」が消えて断定になった／否定が足された
  4. 方向の反転         増える↔減る、高い↔低い
  5. 全称化             「大多数」「一部」が「すべて」「常に」になった
  6. 概念の統合         「AとB」と書いたが、原文のどの文にもAとBが並んで出てこない
  7. 他のページの概念   根拠のページに無い、資料の他の箇所の語を持ち込んだ

検出したら、呼び出し側が「原文の文にそのまま置き換える」か「使わない」。
語の重なりで見る検査なので、原文と同じ語・同じ構造で意味だけを変えた文は検出できない（README の限界を参照）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.pdf.extractor import Block

from .attribution import Attributor, _bigrams

# 内容語（2文字以上の漢字・カタカナ・英数字の連なり）
_WORD = re.compile(r"[一-鿿々]{2,}|[ァ-ヶー]{2,}|[A-Za-z][A-Za-z0-9\-]{1,}")
# 文の区切り。箇条書きの記号は別の文にする（スライドの箇条書きを1文に混ぜない）
_SPLIT = re.compile(r"(?<=[。．！？!?])|[\n●◦■□▪•]+")
_CLAUSE = re.compile(r"(?<=[、，,；;。．！？!?])")
_RANGE = re.compile(r"(\d[\d.]*)\s*(?:〜|~|～|–|—|−|から)\s*(\d[\d.]*)")
_NUMBER = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?:〜(\d+(?:\.\d+)?))?(%?)(?![\d])")

# どの資料でも出てくる語。数値の対象としては扱わない
_GENERIC = frozenset(
    "範囲 程度 割合 場合 状態 結果 傾向 影響 関係 内容 部分 全体 以上 以下 以内 以外 一方 今回 本研究 資料 文書 説明 重要 必要 可能 一般 基本 "
    "主要 方法 手法 目的 問題 課題 事例 場面 全員 同士 双方 それぞれ これら それら 各自 ところ 理由 原因 要因 条件 状況 現象 特徴 "
    "一部 大多数 多く ほとんど 以降 以前 現在 最大 最小 平均 合計 全く 人数 件数 回数 個数 台数 金額 数値 比率 値 数 量 率 人 件 名 数量 データ 情報 項目".split()
)
_MAX_DF_RATIO = 0.6        # 資料のこれ以上の割合のブロックに出る語は、ありふれた語（資料の主題そのもの）とみなして対象にしない
_MIN_DF_CAP = 6            # 短い資料では、少なくともこのブロック数までは対象にする（資料の中心概念「本心」「態度」も対象）

# 関係を表す語。「原文に無いのに要約にある」ものを見つける
_CAUSAL = re.compile(r"ため|ので|により|によって|による|ことから|せい|原因|もたらし|引き起こ|生じ|起因|そのため|したがって|従って|ゆえ|影響で")
# 概要は要点を言い換えてまとめる文なので、「ため」「ので」「場合」などの接続は言い換えで現れる。原因を言い切る強い語だけを見る
_CAUSAL_STRONG = re.compile(r"原因|もたらし|引き起こ|起因|せい|ゆえ|影響で")
_CONDITION = re.compile(r"場合|ならば|であれば|なら|れば|たら|限り|のとき|とき|際には|際に|条件")
_COMPARE = re.compile(r"より|比べ|比較|上回|下回|に対して|対して|一方")
# 断定を和らげている語（原文にあって、要約で消えると断定になる）
_HEDGE = re.compile(r"可能性|かもしれ|考えられ|思われ|示唆|とは限らな|とは断定|断定でき|わけではな|おそれ|恐れ|推測|推定|疑われ|傾向|ことがある|場合がある|とみられ|見られ")
_NEG_STRONG = re.compile(r"ではな|ではなく|とは限らな|とは言えな|とは断定|断定でき|わけではな|必ずしも|ことはな|できな|認められな|見られな|みられな|ありません|ていない|ない。?$|なかった")
_NEG_ANY = re.compile(r"ない|なく|なかっ|ず(?![っれ])|ぬ|ません|不可|不能|不要|未[満達]|否定|除[くい外]|できず")
_UP = re.compile(r"増|上昇|高い|高く|高まっ|大きい|大きく|多い|多く|向上|改善|拡大|伸び|上回|強まっ|強い")
_DOWN = re.compile(r"減|低下|低い|低く|小さい|小さく|少ない|少なく|悪化|縮小|下回|下降|弱まっ|弱い|落ち")
_UNIVERSAL = re.compile(r"すべて|全て|常に|必ず|いずれも|どれも|全員|全く|例外なく|一律")
_PARTIAL = re.compile(r"大多数|多くの|ほとんど|一部|少数|一定の|場合がある|ことがある|のみ")
# 「AとB」「AやB」
_CONJ = re.compile(r"([一-鿿々]{2,}|[ァ-ヶー]{2,}|[A-Za-z][A-Za-z0-9\-]{1,})(?:と|や|および|ならびに|・)([一-鿿々]{2,}|[ァ-ヶー]{2,}|[A-Za-z][A-Za-z0-9\-]{1,})(?=の|が|は|も)")

_REPLACE_MAX = 150


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return _RANGE.sub(lambda m: f"{m.group(1)}〜{m.group(2)}", t)


def _flat(text: str) -> str:
    """PDF の行送りで切れた文をつなぐ（日本語どうしの改行は空白なしで連結）。"""
    t = re.sub(r"(?<=[぀-ヿ一-鿿、。）」])\s*\n\s*(?=[぀-ヿ一-鿿])", "", text)
    return t


_SP_A = re.compile(r"(?<=[0-9A-Za-z%])[ \t]+(?=[぀-ヿ一-鿿])")
_SP_B = re.compile(r"(?<=[぀-ヿ一-鿿])[ \t]+(?=[0-9A-Za-z])")


def tidy_pdf_spaces(text: str) -> str:
    """PDFが入れる、英数字と日本語の間の余計な空白を取る（「0.7 から0.8 の間」→「0.7から0.8の間」）。表示の整形だけで、語は変えない。"""
    return _SP_B.sub("", _SP_A.sub("", text))


def _strip_lead_fragment(s: str) -> str:
    """ページをまたいで文頭だけが切れた断片（「見ると、」）を外す。残りが十分に長いときだけ。"""
    m = re.match(r"^[^、。，]{1,6}[、，]", s)
    return s[m.end():] if m and len(s) - m.end() >= 20 else s


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(_flat(text)) if s and s.strip()]


def _words(text: str) -> list[str]:
    return _WORD.findall(_norm(text))


@dataclass
class Issue:
    kind: str        # 検出した変化の種類（利用者向けの短い言葉）
    detail: str = ""


class FidelityChecker:
    def __init__(self, blocks: Iterable[Block], attributor: Attributor | None = None) -> None:
        self._att = attributor
        self._blocks: dict[int, Block] = {}
        self._page_blocks: dict[int, list[int]] = {}
        self._sents: dict[int, list[str]] = {}
        self._df: dict[str, set[int]] = {}
        self._doc_sents: list[str] = []
        for b in blocks:
            self._blocks[b.id] = b
            self._page_blocks.setdefault(b.page, []).append(b.id)
            ss = split_sentences(b.text)
            self._sents[b.id] = ss
            self._doc_sents.extend(_norm(s) for s in ss)
            for w in set(_words(b.text)):
                self._df.setdefault(w, set()).add(b.id)
        self._df_cap = max(_MIN_DF_CAP, int(len(self._blocks) * _MAX_DF_RATIO))

    # ------------------------------------------------------------ 公開

    def check(self, claim: str, ids: Iterable[int] | None = None, *, texts: list[str] | None = None) -> list[Issue]:
        """claim と根拠（ブロックID、または文章の一覧）を比べ、意味が変わった可能性のある点を返す。空なら問題なし。"""
        evidence = self._evidence_sentences(claim, ids, texts)
        if not evidence:
            return []
        issues: list[Issue] = []
        issues += self._numbers(claim, evidence, ids, texts)
        issues += self._relations(claim, evidence, self._primary(claim, evidence), strict=texts is None)
        if texts is None:                                       # 概要の「AとBについて」は列挙なので対象外
            issues += self._conjunctions(claim, evidence)
        if ids is not None:
            issues += self._imported(claim, ids)
        return issues

    def restore(self, claim: str, ids: Iterable[int]) -> str | None:
        """ずれた要約の代わりに、根拠の原文から最も近い箇所をそのまま取り出す。適切なものが無ければ None。

        要約が複数の数値・文にまたがるときは、数値を含む文を（最大2文まで）原文の順に並べる。
        """
        ordered = [s for i in ids for s in self._sents.get(i, [])]
        if not ordered:
            return None
        want = _bigrams(claim)
        if not want:
            return None
        nums = [m.group(0).rstrip("%") for m in _NUMBER.finditer(_norm(claim)) if self._number_worth(m)]

        def has(sent: str, n: str) -> bool:
            return bool(re.search(rf"(?<![\d.]){re.escape(n)}(?!\d)", _norm(sent)))

        def score(sent: str, need: list[str]) -> tuple[float, float]:
            overlap = len(_bigrams(sent) & want) / max(1, len(want))
            return (sum(1 for n in need if has(sent, n)) + overlap, overlap)

        first = max(ordered, key=lambda x: score(x, nums))
        if score(first, nums)[1] < 0.25:
            return None
        picked = [first]
        left = [n for n in nums if not has(first, n)]
        if left:
            second = max((x for x in ordered if x is not first), key=lambda x: score(x, left), default=None)
            if second is not None and any(has(second, n) for n in left) and score(second, left)[1] >= 0.15:
                picked.append(second)
        picked.sort(key=ordered.index)
        out = "".join(self._window(p, set(nums)) for p in picked) if len(picked) > 1 else self._window(first, set(nums))
        out = tidy_pdf_spaces(out)
        if self._att is not None and self._att.coverage_in(claim, [out]) < 0.3:
            return None
        return out

    # ------------------------------------------------------------ 根拠の文を選ぶ

    def _evidence_sentences(self, claim: str, ids: Iterable[int] | None, texts: list[str] | None) -> list[str]:
        heads: list[str] = []
        if texts is not None:
            pool = [s for t in texts for s in split_sentences(t)]
        else:
            pool = []
            for i in (ids or []):
                prev = self._blocks.get(i - 1)
                if prev is not None and prev.page == getattr(self._blocks.get(i), "page", None) and (prev.is_heading or len(prev.text) <= 24):
                    heads += self._sents.get(prev.id, [])         # 直前の見出し（「誤解防止」など）は、選別せず文脈に加える
                pool += self._sents.get(i, [])
        if not pool:
            return []
        want = _bigrams(claim)
        if not want:
            return pool[:3]
        weight = (lambda bg: self._att._weight(bg)) if self._att is not None else (lambda bg: 1.0)
        total = sum(weight(bg) for bg in want) or 1.0
        remaining = set(want)
        chosen: list[str] = []
        for _ in range(3):
            best, gain = None, 0.0
            for s in pool:
                if s in chosen:
                    continue
                g = sum(weight(bg) for bg in _bigrams(s) & remaining)
                if g > gain:
                    best, gain = s, g
            if best is None or gain < 0.10 * total:
                break
            chosen.append(best)
            remaining -= _bigrams(best)
        return chosen + [h for h in heads if h not in chosen]

    def _window(self, sentence: str, nums: set[str]) -> str:
        """文が長いときは、数値（なければ文頭）を含む節のまとまりだけを取り出す。原文の部分文字列のまま。"""
        s = sentence.strip()
        if len(s) <= _REPLACE_MAX:
            s = _strip_lead_fragment(s)
            return s if s.endswith(("。", "．", ".", "！", "？")) else s + "。"
        clauses = [c for c in _CLAUSE.split(s) if c]
        k = next((j for j, c in enumerate(clauses) if any(n in _norm(c) for n in nums)), 0)
        lo = hi = k
        while True:
            grew = False
            if lo > 0 and len("".join(clauses[lo - 1:hi + 1])) <= _REPLACE_MAX:
                lo -= 1; grew = True
            if hi < len(clauses) - 1 and len("".join(clauses[lo:hi + 2])) <= _REPLACE_MAX:
                hi += 1; grew = True
            if not grew:
                break
        while lo < k and len(clauses[lo]) <= 6 and clauses[lo].endswith(("、", "，")):
            lo += 1                                              # 「見ると、」のような、前のページから続く断片は外す
        while hi > k and "".join(clauses[lo:hi + 1]).count("（") + "".join(clauses[lo:hi + 1]).count("(") > \
                "".join(clauses[lo:hi + 1]).count("）") + "".join(clauses[lo:hi + 1]).count(")"):
            hi -= 1                                              # 括弧（引用）の途中で切らない
        out = "".join(clauses[lo:hi + 1]).rstrip("、，,；;")
        return out if out.endswith(("。", "．", ".")) else out + "。"

    # ------------------------------------------------------------ 1. 数値の対象

    @staticmethod
    def _number_worth(m: re.Match) -> bool:
        a, b, pct = m.group(1), m.group(2), m.group(3)
        return bool(b or pct or "." in a or len(a) >= 2)

    def _specific(self, word: str) -> bool:
        """資料の中に出る、内容を表す語。ありふれた語（一覧 _GENERIC・ほとんどのブロックに出る語）や、資料に無い言い換えは対象にしない。

        「本心」「態度」のように、資料の中心概念でも対象にする（数値が指す対象のすり替えは、こうした語で起きる）。
        """
        if word in _GENERIC:
            return False
        df = len(self._df.get(word, ()))
        return 1 <= df <= self._df_cap

    def _numbers(self, claim: str, evidence: list[str], ids, texts) -> list[Issue]:
        issues: list[Issue] = []
        c_norm = _norm(claim)
        c_clauses = [c for c in _CLAUSE.split(c_norm) if c]
        pool = [_norm(s) for s in evidence]
        for m in _NUMBER.finditer(c_norm):
            if not self._number_worth(m):
                continue
            tok = m.group(0)
            pat = re.compile(rf"(?<![\d.]){re.escape(tok.rstrip('%'))}(?!\d)")
            clause = next((c for c in c_clauses if m.group(0) in c), "")
            subject = [w for w in _words(clause) if self._specific(w)]
            if not subject:
                continue
            ctx_list = self._number_contexts(pat, pool)
            if not ctx_list:
                # 根拠の近傍の文に数値が無ければ、引用ブロック全体で探す。それでも無ければ数値の実在確認（別の検査）に任せる
                full = [_norm(s) for i in (ids or []) for s in self._sents.get(i, [])] if texts is None else []
                ctx_list = self._number_contexts(pat, full)
                if not ctx_list:
                    continue
            best_missing: list[str] | None = None
            for ctx in ctx_list:
                have = ctx
                missing = [w for w in subject if not self._in_text(w, have)]
                if best_missing is None or len(missing) < len(best_missing):
                    best_missing = missing
            if best_missing:
                issues.append(Issue("数値の対象がずれている", f"{tok}：{'・'.join(dict.fromkeys(best_missing))}"))
        return issues

    @staticmethod
    def _number_contexts(pat: re.Pattern, sentences: list[str]) -> list[str]:
        """原文で数値が出る節（主語が前の節にあるときは、その節も）の文字列の一覧。"""
        out: list[str] = []
        for s in sentences:
            clauses = [c for c in _CLAUSE.split(s) if c]
            for j, c in enumerate(clauses):
                if pat.search(c):
                    ctx = c
                    if len([w for w in _WORD.findall(c)]) < 2 and j > 0:
                        ctx = clauses[j - 1] + c
                    out.append(ctx)
        return out

    @staticmethod
    def _in_text(word: str, text: str) -> bool:
        if word in text:
            return True
        if len(word) >= 3 and word[-1] in "率化性的者度数量法型" and word[:-1] in text:
            return True                                          # 「理解率」と「理解した割合」のような派生語の言い換え
        bg = _bigrams(word)
        return bool(bg) and len(bg & _bigrams(text)) / len(bg) >= 0.6

    # ------------------------------------------------------------ 2〜5. 関係・限定・方向・全称

    @staticmethod
    def _primary(claim: str, evidence: list[str]) -> list[str]:
        """要約の元になった文（最も近い1文と、同じ数値を含む文）。限定・否定の「脱落」は、この文に対してだけ見る。

        近傍の数文を全部見ると、別の話題の文にある「可能性」「ない」を拾って、誤って警告するため。
        """
        nums = [m.group(0).rstrip("%") for m in _NUMBER.finditer(_norm(claim))]
        out = evidence[:1]
        for sent in evidence[1:]:
            n = _norm(sent)
            if any(re.search(rf"(?<![\d.]){re.escape(x)}(?!\d)", n) for x in nums if len(x) >= 2):
                out.append(sent)
        return out

    def _relations(self, claim: str, evidence: list[str], primary: list[str], *, strict: bool = True) -> list[Issue]:
        ev = "".join(_norm(s) for s in evidence)            # 足された語は、近傍の全文に無いときだけ指摘する（緩い）
        pv = "".join(_norm(s) for s in primary)             # 消えた語は、元の文に有るときだけ指摘する（厳しい）
        c = _norm(claim)
        issues: list[Issue] = []
        kinds = (("原文に無い因果関係", _CAUSAL), ("原文に無い条件", _CONDITION), ("原文に無い比較", _COMPARE)) if strict \
            else (("原文に無い因果関係", _CAUSAL_STRONG),)
        for name, pat in kinds:
            if pat.search(c) and not pat.search(ev):
                if pat is _CAUSAL and self._noun_purpose_supported(c, ev):
                    continue
                issues.append(Issue(name, pat.search(c).group(0)))
        if _HEDGE.search(pv) and not _HEDGE.search(c) and not _NEG_ANY.search(c):
            issues.append(Issue("断定に変わっている", _HEDGE.search(pv).group(0)))
        if _NEG_STRONG.search(pv) and not _NEG_ANY.search(c):
            issues.append(Issue("否定が落ちている", _NEG_STRONG.search(pv).group(0)))
        elif _NEG_STRONG.search(c) and not _NEG_ANY.search(ev):
            issues.append(Issue("否定が足されている", _NEG_STRONG.search(c).group(0)))
        if _UP.search(c) and not _DOWN.search(c) and _DOWN.search(pv) and not _UP.search(pv):
            issues.append(Issue("方向が逆", "増↔減"))
        elif _DOWN.search(c) and not _UP.search(c) and _UP.search(pv) and not _DOWN.search(pv):
            issues.append(Issue("方向が逆", "減↔増"))
        if _UNIVERSAL.search(c) and not _UNIVERSAL.search(ev) and _PARTIAL.search(ev):
            issues.append(Issue("「一部・多く」が「すべて」に変わっている", _UNIVERSAL.search(c).group(0)))
        return issues

    def _noun_purpose_supported(self, claim: str, ev: str) -> bool:
        """「誤解防止のため、…」のように、見出しや原文の語を目的として添えた形か（動詞＋ため・原因の表現は対象外）。"""
        found = list(re.finditer(r"([一-鿿々ァ-ヶー]{2,8})のため", claim))
        rest = re.sub(r"[一-鿿々ァ-ヶー]{2,8}のため", "", claim)
        return bool(found) and not _CAUSAL.search(rest) and all(self._in_text(m.group(1), ev) for m in found)

    # ------------------------------------------------------------ 6. 概念の統合

    def _conjunctions(self, claim: str, evidence: list[str]) -> list[Issue]:
        issues: list[Issue] = []
        c = _norm(claim)
        ev = "".join(_norm(s) for s in evidence)
        for m in _CONJ.finditer(c):
            a, b = m.group(1), m.group(2)
            if not (self._specific(a) and self._specific(b)):
                continue
            if any(self._in_text(a, s) and self._in_text(b, s) for s in self._doc_sents):
                continue                                         # 原文のどこかの1文に、AもBも出てくる
            if self._in_text(a, ev) or self._in_text(b, ev):
                issues.append(Issue("別々の概念をひとまとめにしている", f"{a}・{b}"))
        return issues

    # ------------------------------------------------------------ 7. 他のページの概念

    def _imported(self, claim: str, ids: Iterable[int]) -> list[Issue]:
        pages = {self._blocks[i].page for i in ids if i in self._blocks}
        if not pages:
            return []
        here = {b for p in pages for b in self._page_blocks.get(p, [])}
        page_text = "".join(_norm(self._blocks[b].text) for b in here)
        foreign: list[str] = []
        for w in dict.fromkeys(_words(claim)):
            if not self._specific(w):
                continue
            where = self._df.get(w, set())
            if where and not (where & here) and not self._in_text(w, page_text):
                foreign.append(w)
        return [Issue("根拠のページに無い、他の箇所の語", "・".join(foreign))] if foreign else []
