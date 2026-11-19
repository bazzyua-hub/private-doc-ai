"""モデル出力の解釈と、参照IDの検証（F-05）。

- モデルが返した [B12] / [S3] 形式の参照を取り出す
- 渡した範囲に存在しない番号は捨てる（存在確認はプログラム側。モデルの出力を信用しない）
- 参照先が決まらない主張は「参照箇所未特定」にする
- 主張中の数値が根拠ブロックに実在するかを機械的に確認する

LLM に依存しない純粋関数だけで構成し、単体テストできるようにしてある。
"""
from __future__ import annotations

import re
import unicodedata

from typing import Callable

from .schemas import STATUS_INFERRED, STATUS_OK, STATUS_OVERALL, STATUS_RANGE, STATUS_UNRESOLVED, Claim, Section

# ---------------------------------------------------------------- 出力の前処理

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*$", re.M)


def clean_model_output(text: str) -> str:
    """思考タグ・コードフェンスを除去する（非思考モードでも念のため）。"""
    t = _THINK.sub("", text)
    if "</think>" in t:                      # 開始タグだけ欠けた場合
        t = t.split("</think>", 1)[1]
    t = _FENCE.sub("", t)
    return t.strip()


# ---------------------------------------------------------------- 参照ラベル

_TOKEN = r"[BbSs]\s*\d+(?:\s*[-–~]\s*[BbSs]?\s*\d+)?"
_NEXT = r"[BbSs]?\s*\d+(?:\s*[-–~]\s*[BbSs]?\s*\d+)?"
_REF_GROUP = re.compile(rf"[\[［【(（]\s*({_TOKEN}(?:\s*[,、，・]\s*{_NEXT})*)\s*[\]］】)）]")
_REF_ITEM = re.compile(r"([BbSs])?\s*(\d+)(?:\s*[-–~]\s*([BbSs])?\s*(\d+))?")


def parse_refs(text: str, prefix: str) -> tuple[str, list[int], int]:
    """文中の参照を取り除き、(参照を除いた文, prefixに一致する番号のリスト, 別種の参照の数) を返す。"""
    found: list[int] = []
    wrong = 0

    def handle(m: re.Match) -> str:
        nonlocal wrong
        letter = ""
        for im in _REF_ITEM.finditer(m.group(1)):
            letter = (im.group(1) or letter or "").upper()
            a = int(im.group(2))
            b = int(im.group(4)) if im.group(4) else a
            nums = range(a, b + 1) if 0 <= b - a <= 20 else [a]
            if letter == prefix:
                found.extend(nums)
            else:
                wrong += 1
        return ""

    cleaned = _REF_GROUP.sub(handle, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([。、．，.,])", r"\1", cleaned)
    return cleaned, found, wrong


# ---------------------------------------------------------------- セクション分割

_CANON = {
    "概要": "概要",
    "主要ポイント": "主要ポイント",
    "主なポイント": "主要ポイント",
    "詳細": "詳細",
    "やること・手順": "やること・手順",
    "やること": "やること・手順",
    "手順": "やること・手順",
    "作業手順": "やること・手順",
    "注意点・限界": "注意点・限界",
    "注意点と限界": "注意点・限界",
    "注意点": "注意点・限界",
    "限界": "注意点・限界",
}
_NAMES = "|".join(sorted(_CANON, key=len, reverse=True))
_HEAD = re.compile(
    rf"^\s*(?:#{{1,6}}\s*|\*\*\s*|■\s*|【\s*|\d+[.)]\s*)?({_NAMES})\s*(?:\*\*|】)?\s*(?:[:：]\s*(.*?))?\s*(?:\*\*)?\s*$"
)
_BULLET_LINE = re.compile(r"^(\s*)(?:[-*]\s+|[•・●○▪]\s*|\d+[.)．）]\s*)(.+)$")


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m and len(line.strip()) <= 40 + len(m.group(2) or ""):
            sections.append((_CANON[m.group(1)], []))
            if m.group(2):
                sections[-1][1].append(m.group(2))
            continue
        if sections:
            sections[-1][1].append(line)
    return sections


def lines_to_items(lines: list[str], *, join_paragraphs: bool = True) -> list[str]:
    """行の並びを「主張1件ずつの文字列」にする。箇条書きは1行1件、地の文は段落ごとに1件。

    join_paragraphs=False なら、箇条書き記号の有無にかかわらず1行を1件にする（部分要約の解析用。
    小型モデルは記号をつけずに `[B3] …` だけで行を始めることがあるため）。
    """
    items: list[str] = []
    para: list[str] = []
    last_bullet = False

    def join(parts: list[str]) -> str:
        out = parts[0]
        for p in parts[1:]:
            out += p if (out[-1:] and (_is_wide(out[-1]) or _is_wide(p[:1]))) else " " + p
        return out

    def flush() -> None:
        if para:
            items.append(join(para))
            para.clear()

    for ln in lines:
        s = ln.strip()
        if not s:
            flush()
            last_bullet = False
            continue
        m = _BULLET_LINE.match(ln)
        if not join_paragraphs:
            items.append(m.group(2).strip() if m else s)
            continue
        if m:
            flush()
            items.append(m.group(2).strip())
            last_bullet = True
        elif last_bullet and (len(ln) - len(ln.lstrip())) >= 2 and items:
            items[-1] = join([items[-1], s])          # 箇条書きの折り返し行
        else:
            para.append(s)
            last_bullet = False
    flush()
    return [re.sub(r"\*\*", "", i).strip() for i in items if i.strip()]


def _is_wide(ch: str) -> bool:
    return bool(ch) and unicodedata.east_asian_width(ch) in ("W", "F")


# ---------------------------------------------------------------- 数値の確認

_NUM = re.compile(r"(?<![0-9A-Za-z_.])\d[\d,]*(?:\.\d+)?%?")   # 日本語の直後の数字も拾う（\w は使わない）
_NO_REF_OK = re.compile(r"^(?:.{0,24}[：:]\s*)?(特になし|記載なし|該当なし|要点なし|なし|特に(?:ありません|なし))[。.]?$")


# 小型モデルが、複数の要点を1行に空白区切りで詰め込むことがある。日本語どうしに挟まれた空白で分ける。
# 左側に数字は含めない（「2015 年」「100 層」のように、数字と単位の間に空白が入るPDFが多いため）
_CRAM_SPLIT = re.compile(r"(?<=[぀-ヿ一-鿿％%）)」』。々ー])[ \t]+(?=[぀-ヿ一-鿿])")


def split_crammed(text: str) -> list[str]:
    """長い1行が「空白区切りの複数の要点」に見える場合だけ分ける。通常の文は分けない。"""
    if len(text) < 60:
        return [text]
    parts = [p.strip() for p in _CRAM_SPLIT.split(text) if p.strip()]
    if len(parts) < 3 or any(len(p) < 6 for p in parts):
        return [text]
    return parts


_LABEL_PREFIX = re.compile(r"^要点\s*\d+\s*[：:]\s*")
_INSTRUCTION_ECHO = re.compile(r"(?:と|を|に)書く[。.]?$")     # 「…は「特になし」と書く。」のようにプロンプトの指示文をそのまま写した行


# 「〜を説明する」「〜が記載されている」だけで、中身が無い項目（資料を説明しているだけで、内容を伝えない）
_VACUOUS = re.compile(
    r"(?:を|が|は|に関して|について)?(?:示す|示している|説明する|説明している|解説する|記載する|記載している|記載されている|"
    r"記載されています|記載しています|記述している|記述しています|述べている|紹介する|紹介している)[。.]?$"
)


def is_vacuous(text: str) -> bool:
    t = text.strip()
    return len(t) <= 40 and not re.search(r"\d", t) and bool(_VACUOUS.search(t))


def normalize_for_numbers(s: str) -> str:
    t = unicodedata.normalize("NFKC", s)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return re.sub(r"\s+%", "%", t)


def check_numbers(claim_text: str, source_text: str) -> list[str]:
    """主張中の数値（2桁以上、または小数・%付き）が根拠テキストに実在するか確認し、見当たらないものを返す。"""
    src = normalize_for_numbers(source_text)
    missing: list[str] = []
    for m in _NUM.finditer(normalize_for_numbers(claim_text)):
        tok = m.group(0)
        digits = re.sub(r"\D", "", tok)
        if len(digits) < 2 and "." not in tok and not tok.endswith("%"):
            continue
        if not re.search(rf"(?<![\d.]){re.escape(tok)}(?!\d)", src) and tok not in missing:
            missing.append(tok)
    return missing


# ---------------------------------------------------------------- 検証つきの組み立て

def resolve_sections(
    raw_sections: list[tuple[str, list[str]]],
    *,
    prefix: str,
    allowed: dict[int, set[int]],
    coarse: set[int],
    source_of: Callable[[list[int]], str],
    document_text: str,
    attribute: Callable[[str], list[int]] | None = None,
    support: Callable[[str, list[int]], float] | None = None,
    min_support: float = 0.5,
) -> list[Section]:
    """モデルの主張を Claim に変換し、参照の存在確認・解決と数値確認を行う。

    support : 主張が引用先の本文で説明できる割合を返す関数。小型モデルは存在する別の番号を書くことがあり、
              番号の存在確認だけでは見抜けないため、内容の照合で検算する（min_support 未満なら引用を信用しない）
    allowed: 参照番号 -> 根拠となるブロックID集合（プログラムが保持している対応表）
    coarse : ブロックまで特定できず、範囲（ページ）しか分からない参照番号
    source_of: 引用したブロックID群から、数値確認に使う根拠テキストを返す関数
               （モデルが同じページの隣のブロックを引用することは多いため、ページ単位で見る）
    """
    sections: list[Section] = []
    for title, lines in raw_sections:
        claims: list[Claim] = []
        # 概要は文章なので折り返しを結合する。箇条書きの節は、記号が無くても1行を1項目として扱う
        # （記号なしの行を結合すると、複数の要点が1つにくっついてしまう）
        entries: list[tuple[str, list[int]]] = []
        for raw in lines_to_items(lines, join_paragraphs=(title == "概要")):
            text, nums, _wrong = parse_refs(raw, prefix)
            text = _LABEL_PREFIX.sub("", text)
            if not text or _INSTRUCTION_ECHO.search(text) or (title != "概要" and is_vacuous(text)):
                continue
            pieces = split_crammed(text) if title != "概要" else [text]
            entries.extend((pc, nums) for pc in pieces)
        for text, nums in entries:
            valid = [n for n in dict.fromkeys(nums) if n in allowed]
            ids = sorted({i for n in valid for i in allowed[n]})
            if not valid:
                if _NO_REF_OK.match(text):
                    status = STATUS_OK
                elif title == "概要" and not nums:
                    status = STATUS_OVERALL          # 概要は文書全体の要約なので、個別の参照が無いこと自体は許容する
                else:
                    status = STATUS_UNRESOLVED
            elif all(n in coarse for n in valid):
                status = STATUS_RANGE
            else:
                status = STATUS_OK
            if (title != "概要" and status in (STATUS_OK, STATUS_RANGE) and ids and support is not None
                    and not _NO_REF_OK.match(text) and support(text, ids) < min_support):
                # 存在する番号だが、内容が合わない（別の番号を書いた）。本文から探し直し、見つからなければ未特定にする
                inferred = attribute(text) if attribute is not None else []
                if inferred:
                    ids, status = inferred, STATUS_INFERRED
                else:
                    ids, status = [], STATUS_UNRESOLVED
                valid = []
            if status == STATUS_UNRESOLVED and attribute is not None:
                inferred = attribute(text)                 # モデルが参照を付けなかった文は、本文との照合で探す
                if inferred:
                    ids, status = inferred, STATUS_INFERRED
            if ids:
                source = source_of(ids)
            else:
                source = document_text          # 参照先不明の主張は文書全体に対して数値の実在だけ確認する
            unverified = [] if _NO_REF_OK.match(text) else check_numbers(text, source)
            claims.append(
                Claim(
                    text=text,
                    labels=[f"{prefix}{n}" for n in valid],
                    block_ids=ids,
                    status=status,
                    unverified_numbers=unverified,
                )
            )
        sections.append(Section(title=title, claims=claims))
    return _drop_duplicates(sections)


def _norm_claim(t: str) -> str:
    return re.sub(r"[\s。．.、,]+", "", unicodedata.normalize("NFKC", t))


def _drop_duplicates(sections: list[Section]) -> list[Section]:
    """別の節に同じ文があれば、後のものを捨てる（小型モデルは「詳細」に主要ポイントをそのまま写すことがある）。"""
    seen: set[str] = set()
    out: list[Section] = []
    for sec in sections:
        kept = []
        for c in sec.claims:
            k = _norm_claim(c.text)
            if k in seen and len(k) >= 8:
                continue
            seen.add(k)
            kept.append(c)
        if sec.claims and not kept:
            continue                                    # 重複だけだった節は出さない
        sec.claims = kept
        out.append(sec)
    return out


# ---------------------------------------------------------------- 簡体字の混入検出

# 日本語の漢字としては使われない、中国語（簡体字）だけの字形。小型の中国語系モデルが日本語に混ぜることがある。
_SIMPLIFIED = set("过复发开关请专业应现问动变类单东车长门间员产标论证议处导边组织网络线统计质规库传输备测维护认设调优为这个们说时对")


def find_simplified(text: str) -> list[str]:
    return sorted({ch for ch in text if ch in _SIMPLIFIED})
