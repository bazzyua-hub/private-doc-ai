"""抽出ブロックの整形・分割・選択（F-02 後半 / F-03 / 5.2）。

LLM にもPDFライブラリにも依存しない純粋関数だけで構成し、単体テストしやすくしてある。
"""
from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field, replace

from .extractor import Block

# ---------------------------------------------------------------- 文字・トークン見積り

_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
_DIGIT = re.compile(r"\d")
_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_cjk(ch: str) -> bool:
    return bool(ch) and _CJK.match(ch) is not None


def estimate_tokens(text: str) -> int:
    """トークライザを使わない保守的な見積り（日本語は1文字=1トークン、他は3文字=1トークン）。

    実測との比は RunMetrics.token_estimate_ratio で確認できる。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    digits = len(_DIGIT.findall(text))
    other = len(text) - cjk - digits
    return int(cjk + digits + other / 3.0) + 1


# ---------------------------------------------------------------- 整形

_BULLET = re.compile(r"^\s*([•・●○■□▪▫◆◇▶▸►\-–—*]|[0-9０-９]{1,2}[.)．）]|[(（][0-9０-９]{1,2}[)）]|[①-⑳])\s*")
_SENTENCE_END = ("。", "．", ".", "！", "？", "!", "?")


def _looks_like_list(lines: list[str]) -> bool:
    bullets = sum(1 for ln in lines if _BULLET.match(ln))
    if bullets >= 2:
        return True
    if len(lines) >= 3:
        mean_len = sum(len(ln.strip()) for ln in lines) / len(lines)
        ends = sum(1 for ln in lines if ln.rstrip().endswith(_SENTENCE_END))
        return mean_len <= 30 and ends == 0
    return False


def normalize_text(raw: str) -> str:
    """改行・空白を整える。日本語どうしの改行は詰め、英語は空白に置き換え、箇条書きは行を保つ。"""
    s = unicodedata.normalize("NFC", raw)
    s = _CTRL.sub("", _INVISIBLE.sub("", s)).replace(" ", " ").replace("　", " ")
    lines = [ln.strip() for ln in s.split("\n")]
    lines = [re.sub(r"[ \t]+", " ", ln) for ln in lines if ln]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    if _looks_like_list(lines):
        return "\n".join(lines)
    out = lines[0]
    for nxt in lines[1:]:
        a, b = out[-1], nxt[0]
        if is_cjk(a) or is_cjk(b):
            out += nxt
        elif a == "-" and b.islower() and len(out) >= 2 and out[-2].isalpha():
            out = out[:-1] + nxt            # 行末ハイフンによる単語分割
        else:
            out += " " + nxt
    return out.strip()


_NOISE = [
    re.compile(r"^[-–—\s]*\d{1,4}[-–—\s]*$"),                       # 12 / - 12 -
    re.compile(r"^(page|p\.?)\s*\d{1,4}(\s*(/|of)\s*\d{1,4})?$", re.I),
    re.compile(r"^\d{1,4}\s*/\s*\d{1,4}$"),
]


def is_noise(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text).strip()
    if len(t) <= 1:
        return True
    return any(p.match(t) for p in _NOISE)


def _repeat_key(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^(?:# )+|(?: #)+$", "", t)          # ページ番号つきフッターと、番号の無い1ページ目を同じ扱いにする


# ---------------------------------------------------------------- 見出し・文書種別

_SECTION_KEYWORDS = {
    "abstract": r"abstract|要旨|抄録",
    "introduction": r"introduction|はじめに|序論|緒言",
    "method": r"methods?|methodology|materials and methods|方法|研究方法|手法",
    "results": r"results?|結果",
    "discussion": r"discussion|考察",
    "conclusion": r"conclusions?|結論|おわりに|まとめ",
    "references": r"references|bibliography|参考文献|引用文献",
}
_SECTION_RE = {
    k: re.compile(rf"^\s*(?:第?[0-9一二三四五六七八九十IVX]+[章節.．)）:：]?\s*)?(?:{v})\s*[:：]?\s*$", re.I)
    for k, v in _SECTION_KEYWORDS.items()
}
_APPENDIX_RE = re.compile(r"^\s*(?:appendix|appendices|付録|補遺)\b", re.I)
_CHAPTER_RE = re.compile(r"^\s*(第\s*[0-9一二三四五六七八九十]+\s*[章節回]|[0-9]+(\.[0-9]+)*[.\s]\s*\S)")


def section_key(text: str) -> str | None:
    t = unicodedata.normalize("NFKC", text).strip()
    if len(t) > 40:
        return None
    for key, rx in _SECTION_RE.items():
        if rx.match(t):
            return key
    return None


def _body_font_size(blocks: list[Block]) -> float:
    sized = [(b.font_size, len(b.text)) for b in blocks if b.font_size > 0]
    if not sized:
        return 10.0
    long_ones = [s for s in sized if s[1] >= 30] or sized
    # 文字数で重み付けした中央値
    expanded = sorted(long_ones)
    total = sum(n for _, n in expanded)
    acc = 0
    for size, n in expanded:
        acc += n
        if acc >= total / 2:
            return size
    return expanded[-1][0]


def _is_heading(b: Block, body: float) -> bool:
    t = b.text.strip()
    if "\n" in t or not t or len(t) > 120 or t.endswith(_SENTENCE_END):
        return False
    if section_key(t):
        return True
    if b.font_size >= body * 1.15 and len(t) <= 80:
        return True
    return False


def detect_doc_kind(blocks: list[Block]) -> str:
    """見出しの並びから学術論文らしさを判定する（"paper" / "general"）。"""
    keys = {section_key(b.text) for b in blocks if b.is_heading}
    keys.discard(None)
    return "paper" if len(keys) >= 3 else "general"


# ---------------------------------------------------------------- クリーニング

@dataclass
class CleanResult:
    blocks: list[Block]
    doc_kind: str
    warnings: list[str] = field(default_factory=list)
    body_font_size: float = 10.0


def clean_blocks(blocks: list[Block], *, drop_references: bool = True) -> CleanResult:
    """整形 → ヘッダー/フッター/ページ番号の除去 → 見出し判定 → 参考文献以降の除去。"""
    warnings: list[str] = []
    n_pages = max((b.page for b in blocks), default=0)

    # 1. 整形とノイズ除去
    norm: list[Block] = []
    for b in blocks:
        text = normalize_text(b.text)
        if not text or is_noise(text):
            continue
        norm.append(replace(b, text=text))

    # 2. 全ページで繰り返されるヘッダー/フッターの除去
    if n_pages >= 3:
        pages_by_key: dict[str, set[int]] = {}
        for b in norm:
            if _in_margin(b):
                pages_by_key.setdefault(_repeat_key(b.text), set()).add(b.page)
        threshold = max(3, math.ceil(0.4 * n_pages))
        repeated = {k for k, pages in pages_by_key.items() if len(pages) >= threshold}
        if repeated:
            norm = [b for b in norm if not (_in_margin(b) and _repeat_key(b.text) in repeated)]

    # 2b. 連続するページで同じ内容のブロック（スライドのアニメーション送りで、同じ文が何枚も続く）を1つにする
    norm = _drop_repeated_across_pages(norm)

    # 2c. 日程表（日付が並ぶ表）は、資料の内容ではなく概要を誤らせるので除く
    kept_nonsched = [b for b in norm if not _is_schedule_table(b.text)]
    if len(kept_nonsched) != len(norm):
        warnings.append("日程表と思われる部分は要約の対象から除外しました。")
        norm = kept_nonsched

    # 3. 見出し判定
    body = _body_font_size(norm)
    norm = [replace(b, is_heading=_is_heading(b, body)) for b in norm]
    kind = detect_doc_kind(norm)

    # 4. 参考文献以降を除去（付録の見出しが現れたら再開）
    if drop_references:
        kept: list[Block] = []
        in_refs = False
        dropped = 0
        for b in norm:
            if b.is_heading and section_key(b.text) == "references":
                in_refs = True
            elif in_refs and b.is_heading and _APPENDIX_RE.match(b.text):
                in_refs = False
            if in_refs:
                dropped += 1
            else:
                kept.append(b)
        if dropped:
            warnings.append("参考文献の部分は要約の対象から除外しました。")
        norm = kept

    return CleanResult(blocks=norm, doc_kind=kind, warnings=warnings, body_font_size=body)


_DATE_LINE = re.compile(r"^\s*(?:\d{1,2}\s*/\s*\d{1,2}|\d{4}\s*[/年.-]\s*\d{1,2}\s*[/月.-]\s*\d{0,2}日?|\d{1,2}月\d{1,2}日)\s*$")


def _is_schedule_table(text: str, min_lines: int = 8, min_dates: int = 6) -> bool:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < min_lines:
        return False
    return sum(1 for ln in lines if _DATE_LINE.match(ln)) >= min_dates


def _drop_repeated_across_pages(blocks: list[Block], max_gap: int = 2, min_chars: int = 6) -> list[Block]:
    """直前の数ページ以内に同じ文のブロックがあれば捨てる（最初の出現を残す）。

    PowerPoint等から書き出したスライドは、アニメーションの段階ごとに1ページずつ出力され、タイトルと
    本文が同じページが続く。そのまま要約すると同じ内容に予算と時間を使ってしまう。
    """
    last_page: dict[str, int] = {}
    kept: list[Block] = []
    for b in blocks:
        key = re.sub(r"\s+", "", b.text)
        seen = last_page.get(key)
        last_page[key] = b.page
        if len(key) >= min_chars and seen is not None and 0 < b.page - seen <= max_gap:
            continue
        kept.append(b)
    return kept


def _in_margin(b: Block, ratio: float = 0.12) -> bool:
    if b.page_height <= 0:
        return False
    return b.bbox[3] < b.page_height * ratio or b.bbox[1] > b.page_height * (1 - ratio)


# ---------------------------------------------------------------- 優先度（高速モード・時間予算）

_KEYWORDS = re.compile(
    r"本研究|本稿|目的|結論|まとめ|提案|結果|重要|ポイント|定義|とは|課題|貢献|"
    r"we propose|in this paper|results? show|conclusion|contribution|objective|aim",
    re.I,
)


# 内容の要約には向かない定型の見出し（スケジュール・目次・謝辞など）。その見出しと直後の本文は優先度を下げる
_BOILERPLATE = re.compile(
    r"^\s*(?:スケジュール|日程|目次|アジェンダ|本日の内容|今日の内容|本日の流れ|謝辞|ご清聴|質問|Q\s*&\s*A|"
    r"agenda|outline|contents|schedule|thank you|acknowledg)",
    re.I,
)


_ACTION_END = re.compile(r"(?:なさい|せよ|ください|ましょう|すること|けること|うこと|くこと)[。．.！!）)」』]*$")
_ACTION_HEAD = re.compile(r"^[\s⚫•・●■]*(?:【\s*(?:課題|演習|問題|問)\s*[0-9０-９a-zA-Zａ-ｚ]*\s*】|[①-⑳])")
_CIRCLED_HEAD = re.compile(r"^[\s⚫•・●■]*[①-⑳]")
_DICT_END = re.compile(r"[るうくすつぬぶむぐ][。．.]?$")
_STATE_END = re.compile(r"(?:である|ある|いる|ない|なる)[。．.]?$")
_SENT_BOUNDARY = re.compile(r"[。．\n]+")
_PROMPT_LINE = re.compile(r"^\s*(?:\[[\w.\-]+@[\w.\-]+\][$#]|[\w.\-]+@[\w.\-]+[:~\w/.\-]*[$#]|[A-Za-z]:[\\¥][^>]*>|[$#]\s)", re.M)


def is_action_block(text: str) -> bool:
    """読者に作業を求める文か（「〜しなさい」「〜すること」「【課題1】」「①〜」の番号つき手順）。

    小型モデルは、課題や手順を「内容」として書いたり、コマンド行を写して済ませたりする。作業を求める文は
    言い回しで機械的に見分けられるので、プログラムが印を付けて渡し、種類の判定にも使う。
    """
    t = text.strip()
    if not t:
        return False
    if _CIRCLED_HEAD.match(t) and len(t) <= 400:
        # 番号つきの手順。「①売上が増加した」のような経過・状態の列挙は手順ではない（動詞の終止形で終わるものだけ）
        flat = re.sub(r"\s+", "", t)
        if len(re.findall("[①-⑳]", t)) >= 3 or (_DICT_END.search(flat) and not _STATE_END.search(flat)):
            return True
    if _ACTION_HEAD.match(t) and len(t) <= 400:
        # 「【課題1】(続き)」「【課題】Webサーバの…」のような見出しだけの行は作業の文ではない
        if _ACTION_END.search(re.sub(r"\s+", "", t)):
            return True
    return any(_ACTION_END.search(s.strip()) for s in _SENT_BOUNDARY.split(t) if s.strip())


_GOAL_SENTENCE = re.compile(r"(?:目的|目標|ねらい|狙い|ゴール)\s*(?:は|と|：|:)")
_PAST_END = re.compile(r"(?:した|った|れた|いた|んだ|えた|けた|めた)[。．.]?$")


def is_directive_sentence(sentence: str) -> bool:
    """1文が、読者に作業を明示的に求めているか（「〜しなさい」「〜してください」「〜しましょう」「〜すること」）。

    作業を求める言い回しで終わる文だけを対象にする。「〜した」「〜である」のような、実験・事例・説明の記述は含めない。
    「目的は〜を理解すること」のような学習目標も、読者への作業指示ではないので除く。
    """
    flat = re.sub(r"\s+", "", sentence)
    if len(flat) < 6 or _PAST_END.search(flat):
        return False
    if _GOAL_SENTENCE.search(flat):
        return False
    return bool(_ACTION_END.search(flat))


def is_code_block(text: str) -> bool:
    """コマンドの実行例・設定ファイルの例（日本語の説明を含まない）。"""
    if _PROMPT_LINE.search(text):
        return True
    return not _CJK.search(text) and bool(re.search(r"[</>=/\\-]", text)) and len(text) >= 8


_ADMIN_BLOCK = re.compile(r"^\s*(?:担当|講師|曜日|時限|場所|教室|氏名|所属|連絡先|開講)\s*[:：・]")


def block_priority(blocks: list[Block]) -> dict[int, float]:
    """ブロックIDごとの重要度。見出し・見出し直後の段落・数値・キーワード・冒頭/末尾ページを高くする。"""
    last_page = max((b.page for b in blocks), default=1)
    scores: dict[int, float] = {}
    prev_heading = False
    boiler = False
    for b in blocks:
        s = 0.0
        if b.is_heading:
            s += 3.0
            boiler = bool(_BOILERPLATE.match(b.text))
        elif prev_heading:
            s += 2.0
        if boiler or _ADMIN_BLOCK.match(b.text):
            s -= 4.0
        if _DIGIT.search(b.text):
            s += 1.0
        if _KEYWORDS.search(b.text):
            s += 2.0
        if b.page <= 2:
            s += 1.5
        elif b.page == last_page:
            s += 1.0
        if not b.is_heading and len(b.text) < 15:
            s -= 2.0
        if not b.is_heading and is_action_block(b.text):
            s += 3.0                                    # 読者がやること（課題・手順）は、抜粋でも落とさない
        elif is_code_block(b.text):
            s -= 1.5
        scores[b.id] = s
        prev_heading = b.is_heading
    return scores


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    m = max(cut.rfind(c) for c in ("。", "．", ". ", "！", "？"))
    if m >= max_chars * 0.5:
        cut = cut[: m + 1]
    return cut.rstrip() + "…"


_MIN_BODY_CHARS = 6         # これより短い本文ブロック（「6.」「確認結果」等の図表の断片）は要約の材料にしない
_HEADING_SHARE = 0.15       # 予算のうち、見出し（文脈）に回す上限の割合
_PAGE_SPREAD = 0.7          # 同じページから選ぶほど減点する量（特定のページに偏らないように）


def select_for_fast(blocks: list[Block], *, budget_tokens: int, block_chars: int) -> list[Block]:
    """トークン予算に収まるよう、要約に使うブロックを選ぶ。返す順序は元の文書順。

    1. 本文（見出し以外）を重要度順に選ぶ。同じページから選ぶほど減点し、全体に散らす
    2. 選んだ本文の直前にある見出しだけを、残りの予算で付ける（同じ文言の見出しは1回だけ）

    見出しを単独で高く評価すると、安い見出しばかりで予算を使い切り、本文がほとんど入らない
    （実PDFで40ブロック中31が見出しになった）。見出しは「選んだ本文の文脈」として扱う。
    """
    scores = block_priority(blocks)
    pieces = {b.id: replace(b, text=truncate_text(b.text, block_chars)) for b in blocks}
    cost = {i: estimate_tokens(pc.text) + 6 for i, pc in pieces.items()}      # + [B123] ラベル分

    body_budget = budget_tokens * (1 - _HEADING_SHARE)
    candidates = [b for b in blocks if not b.is_heading and len(b.text.strip()) >= _MIN_BODY_CHARS]
    if not candidates:
        candidates = [b for b in blocks if not b.is_heading]
    per_page: dict[int, int] = {}
    chosen_ids: list[int] = []
    used = 0
    action = {b.id for b in candidates if is_action_block(b.text)}      # 短い作業文（①②③…）は同じページから続けて選んでよい
    while candidates:
        best = max(candidates, key=lambda b: (
            scores[b.id] - (0.0 if b.id in action else _PAGE_SPREAD * per_page.get(b.page, 0)), -b.id))
        candidates.remove(best)
        if used + cost[best.id] > body_budget:
            continue                                        # 入らないものは飛ばし、小さいものを探す
        chosen_ids.append(best.id)
        used += cost[best.id]
        if best.id not in action:
            per_page[best.page] = per_page.get(best.page, 0) + 1

    # 選んだ本文の直前の見出し（重要な本文のものから）
    index = {b.id: n for n, b in enumerate(blocks)}
    heads: list[Block] = []
    for bid in sorted(chosen_ids, key=lambda i: -scores[i]):
        n = index[bid]
        while n > 0 and not blocks[n].is_heading:
            n -= 1
        if blocks[n].is_heading and blocks[n] not in heads:
            heads.append(blocks[n])
    seen_text: set[str] = set()
    for h in heads:
        if used + cost[h.id] > budget_tokens or h.text in seen_text:
            continue
        seen_text.add(h.text)
        chosen_ids.append(h.id)
        used += cost[h.id]

    # 予算が余っていれば、本文をさらに足す
    chosen_set = set(chosen_ids)
    for b in sorted((b for b in blocks if not b.is_heading and b.id not in chosen_set
                     and len(b.text.strip()) >= _MIN_BODY_CHARS),
                    key=lambda b: (-scores[b.id], b.id)):
        if used + cost[b.id] <= budget_tokens:
            chosen_ids.append(b.id)
            used += cost[b.id]
    return [pieces[i] for i in sorted(chosen_ids)]


# ---------------------------------------------------------------- 分割

@dataclass
class Chunk:
    index: int
    blocks: list[Block]
    tokens: int
    heading: str = ""        # このチャンクの直前にあった見出し（文脈用）

    @property
    def ids(self) -> set[int]:
        return {b.id for b in self.blocks}

    @property
    def pages(self) -> tuple[int, int]:
        ps = [b.page for b in self.blocks]
        return (min(ps), max(ps))


_SENT_SPLIT = re.compile(r"(?<=[。．！？!?])|(?<=\.)\s+")


def split_to_limit(text: str, max_tokens: int) -> list[str]:
    """1ブロックが上限を超える場合に、文の境界（なければ文字数）で分ける。"""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sent in (s for s in _SENT_SPLIT.split(text) if s):
        if estimate_tokens(sent) > max_tokens:             # 句点のない長い文
            if cur:
                pieces.append(cur)
                cur = ""
            step = max(30, int(len(sent) * max_tokens / max(1, estimate_tokens(sent))))
            pieces.extend(sent[i:i + step] for i in range(0, len(sent), step))
            continue
        if cur and estimate_tokens(cur + sent) > max_tokens:
            pieces.append(cur)
            cur = sent
        else:
            cur += sent
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def chunk_blocks(blocks: list[Block], max_tokens: int) -> list[Chunk]:
    """見出しと段落の境界を尊重して、トークン上限以内のチャンクに分割する。"""
    expanded: list[Block] = []
    for b in blocks:
        for piece in split_to_limit(b.text, max(50, max_tokens - 20)):
            expanded.append(b if piece == b.text else replace(b, text=piece))

    chunks: list[Chunk] = []
    cur: list[Block] = []
    cur_tokens = 0
    ctx_heading = ""            # ここまでに現れた最後の見出し
    chunk_heading = ""          # 現在のチャンクの直前にあった見出し

    def cost(b: Block) -> int:
        return estimate_tokens(b.text) + 6

    def flush() -> None:
        nonlocal cur, cur_tokens
        if cur:
            chunks.append(Chunk(len(chunks), cur, cur_tokens, chunk_heading))
        cur, cur_tokens = [], 0

    for b in expanded:
        c = cost(b)
        if cur and cur_tokens + c > max_tokens:
            carry: list[Block] = []
            while len(cur) > 1 and cur[-1].is_heading:       # 見出しだけがチャンク末尾に残らないようにする
                carry.insert(0, cur.pop())
            cur_tokens = sum(cost(x) for x in cur)
            flush()
            cur = carry
            cur_tokens = sum(cost(x) for x in cur)
            chunk_heading = ctx_heading
        elif b.is_heading and cur and cur_tokens >= 0.6 * max_tokens:
            flush()
        if not cur:
            chunk_heading = ctx_heading
        cur.append(b)
        cur_tokens += c
        if b.is_heading:
            ctx_heading = b.text
    flush()
    return chunks


def chunk_priority(chunk: Chunk, scores: dict[int, float], n_chunks: int) -> float:
    """時間予算があるときの処理順。平均重要度に、先頭・末尾のチャンクへの加点を足す。"""
    vals = [scores.get(b.id, 0.0) for b in chunk.blocks]
    base = statistics.fmean(vals) if vals else 0.0
    if chunk.index == 0:
        base += 2.0
    if chunk.index == n_chunks - 1:
        base += 1.0
    return base
