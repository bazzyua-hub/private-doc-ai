"""課題・手順の抜き出し（原文から機械的に行う。モデルは使わない）。

実習書・マニュアル・演習資料では、「やること」は決まった書き方で書かれている:
  【課題1】…しなさい。 / ①…する。②…する。 / コマンドの実行例 / ※…を受けること。
これを1.7Bモデルに要約させると、断片になる・コマンドを写す・課題そのものが落ちる、という問題が実測で出た
（「管理者権限で操作を実行する」だけでは、資料を読んでいない人は動けない）。

そこで、プログラムが原文の指示文をそのまま（書き換えずに）課題ごとにまとめる:
  やること（指示文）／条件／手順（①②…）／コマンド／確認・提出（※…）
モデルを使わないので速く、書き換えによる誤りも出ない。根拠のブロックとページは正確に付く。


「読者がやること」として載せるのは、資料が読者に明示的に求めている作業だけ:
- 【課題N】＋「〜しなさい」など、作業を求める言い回しで終わる文
- 番号つきの手順は、その前後に作業を求める文があるときだけ。実験・調査の方法の説明（「〜した」）や、指示の
  無い手順の列挙は、読者への作業指示とは限らないので載せない（載せなかった場所は処理上の注意に出す）
- それ以外の、作業を求める文（「〜してください」「〜すること」）は1文ずつ原文のまま拾う（extract_instructions）
モデルには「手順」を書かせない（書かせると、資料にない作業指示を作ることが実測で確認された）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.pdf.extractor import Block
from app.pdf.text_processor import is_action_block, is_code_block, is_directive_sentence, truncate_text

from .schemas import STATUS_OK, Claim, TaskCard

_CJK_CLASS = "぀-ヿ㐀-䶿一-鿿＀-￯、。，．：；！？「」（）"
_GLYPH = "⚫•・●■□◆◇▶▸►"
_CIRCLED = "①-⑳"

_LABEL = re.compile(rf"^[\s{_GLYPH}]*【\s*(課題|演習|問題|問)\s*([0-9０-９]*)\s*([a-zA-Zａ-ｚ]?)\s*】\s*")
_TITLE = re.compile(rf"^(?:[A-Za-z]{{0,2}}\d*[.．]\s*)?【\s*(課題|演習|問題|問)\s*([0-9０-９]*)\s*([a-zA-Zａ-ｚ]?)\s*】\s*(\S.*)$")
_STEP_HEAD = re.compile(rf"^[\s{_GLYPH}]*[{_CIRCLED}]")
_NOTE_HEAD = re.compile(rf"^[\s{_GLYPH}]*※\s*")
_NOTE_LABEL = re.compile(r"【\s*(?:課題|演習|問題|問)\s*([0-9０-９]+)")
_PROMPT = re.compile(r"\[[\w.\-]+@[\w.\-]+\]\s*[$#]\s*|(?<![\w])[A-Za-z]:[\\¥][^>\s]*>\s*")
_LOG_TAIL = re.compile(r"\s+\d{1,3}(?:\.\d{1,3}){3}\s+-\s+-\s+\[.*$")
_PLACE = re.compile(r"コンソール|TeraTerm|ターミナル|OS")
_PLAIN_NUMBER_TITLE = re.compile(r"^[A-Za-z]?\d{0,3}[.．]?$")

_SCOPE_PAGES = 3            # 課題の指示文から、この枚数より離れたページの手順・コマンドは、その課題のものとして扱わない
_MAX_DO = 3
_MAX_COND = 2
_MAX_LINE = 300


@dataclass
class TaskExtraction:
    cards: list[TaskCard] = field(default_factory=list)
    outline: Claim | None = None
    missing: list[str] = field(default_factory=list)      # 目次にあるが、指示文が見つからなかった課題（例: "課題4"）
    skipped_pages: list[int] = field(default_factory=list)   # 読者への指示と明記されていないため載せなかった、番号つき手順のページ

    def block_ids(self) -> set[int]:
        ids = {i for c in self.cards for i in c.block_ids()}
        if self.outline:
            ids.update(self.outline.block_ids)
        return ids


def _flat(text: str) -> str:
    """ブロックの改行を取り除く。日本語どうしの間は詰め、英数字どうしの間は空白にする。"""
    t = text.replace("　", " ")
    t = re.sub(rf"(?<=[{_CJK_CLASS}])[ \t]*\n[ \t]*(?=[{_CJK_CLASS}])", "", t)
    t = re.sub(r"[ \t]*\n[ \t]*", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip(" " + _GLYPH)


def _key(kind: str, num: str, suffix: str) -> str:
    return unicodedata.normalize("NFKC", f"{kind}{num}{suffix}").lower()


def _number_of(key: str) -> str:
    """"課題3a" -> "3"（課題の種類名と、末尾の英字を除いた番号）。"""
    m = re.search(r"(\d+)[a-z]?$", key)
    return m.group(1) if m else ""


def _claim(text: str, block: Block) -> Claim:
    return Claim(text=text, labels=[f"B{block.id}"], block_ids=[block.id], status=STATUS_OK)


def _bullets(text: str, limit: int = _MAX_LINE) -> list[str]:
    """「⚫」で区切られた箇条書きを行に分ける。短い断片（「対象ディレクトリ」「/var/www/…」）は直前の行に続ける。"""
    parts = [_flat(p) for p in re.split(rf"[{_GLYPH}]", text)]
    parts = [p for p in parts if p]
    lines: list[str] = []
    for p in parts:
        if lines and "。" not in p and "．" not in p:
            lines[-1] += " " + p
        else:
            lines.append(p)
    return [truncate_text(ln, limit) for ln in lines]


def _split_steps(text: str) -> list[str]:
    """①②…を1行ずつに分ける（1ブロックに複数の番号が入っている場合）。"""
    pieces = re.split(rf"(?=[{_CIRCLED}])", text)
    out = [_flat(p) for p in pieces if p.strip()]
    return [truncate_text(p, _MAX_LINE) for p in out if p]


def _strip_label(text: str) -> str:
    return _LABEL.sub("", _flat(text), count=1).strip()


def _commands(block: Block) -> tuple[str, list[str]]:
    """コマンド例のブロックから、（場所の表記, コマンド行のリスト）を取り出す。出力例のログ行は除く。"""
    text = block.text
    ms = list(_PROMPT.finditer(text))
    if not ms:
        return "", []
    head = _flat(text[:ms[0].start()])
    place = head if head and len(head) <= 40 and _PLACE.search(head) else ""
    cmds: list[str] = []
    for i, m in enumerate(ms):
        seg = text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
        line = _LOG_TAIL.sub("", seg.strip().splitlines()[0].strip())
        if line and len(line) <= 160:
            cmds.append(line)
    return place, cmds


@dataclass
class _Draft:
    key: str
    labeled: bool
    start_page: int
    order: int
    title: str = ""
    do: list[Claim] = field(default_factory=list)
    conditions: list[Claim] = field(default_factory=list)
    steps: list[Claim] = field(default_factory=list)
    commands: list[Claim] = field(default_factory=list)
    checks: list[Claim] = field(default_factory=list)
    place: str = ""

    def add_commands(self, block: Block) -> None:
        place, cmds = _commands(block)
        if place and not self.place:
            self.place = place
        self.commands.extend(_claim(c, block) for c in cmds)


def _topics(blocks: list[Block]) -> tuple[dict[str, str], Claim | None, list[str]]:
    """課題の題名（「【課題1】…の作成」）と、全課題を並べた目次（あれば）。"""
    topics: dict[str, str] = {}
    outline: Claim | None = None
    listed: list[str] = []
    for b in blocks:
        t = b.text.strip()
        if "\n" not in t and len(t) <= 90 and not is_action_block(t):
            m = _TITLE.match(t)
            if m and not m.group(4).startswith("(続") and not m.group(4).startswith("（続"):
                topics.setdefault(_key(m.group(1), m.group(2), m.group(3)), _flat(m.group(4)))
        if outline is None and not is_action_block(t):
            found = re.findall(r"【\s*(課題|演習|問題|問)\s*([0-9０-９]+)\s*】\s*([^\n⚫•【]*)", t)
            if len(found) >= 2:
                listed = [_key(k, n, "") for k, n, _ in found]
                outline = Claim(
                    text="／".join(f"{_key(k, n, '')}: {_flat(topic)}" if _flat(topic) else _key(k, n, "")
                                  for k, n, topic in found),
                    labels=[f"B{b.id}"], block_ids=[b.id], status=STATUS_OK,
                )
    return topics, outline, listed


def extract_task_cards(blocks: list[Block], *, max_cards: int = 12) -> TaskExtraction:
    """文書順に並んだブロックから、課題カードを作る。見つからなければ空。"""
    topics, outline, listed = _topics(blocks)
    notes_by_num: dict[str, list[tuple[Claim, Block]]] = {}
    labeled: list[_Draft] = []
    groups: list[_Draft] = []
    current: _Draft | None = None
    group: _Draft | None = None
    last_step_index = -10
    last_role = ""
    n_cond = 0

    index = {b.id: n for n, b in enumerate(blocks)}

    for n, b in enumerate(blocks):
        text = b.text
        if current is not None and b.page > current.start_page + _SCOPE_PAGES:
            current = None
        action = is_action_block(text)
        m_label = _LABEL.match(text)

        if action and m_label:                                  # 【課題1】…しなさい。
            key = _key(m_label.group(1), m_label.group(2), m_label.group(3))
            current = _Draft(key, True, b.page, n)
            current.do.append(_claim(truncate_text(_strip_label(text), _MAX_LINE), b))
            current.checks.extend(c for c, _ in notes_by_num.get(_number_of(key), []))
            labeled.append(current)
            group = None
            last_role, n_cond = "head", 0
            continue

        if b.is_heading and not m_label and not _TITLE.match(text.strip()) \
                and not _PLAIN_NUMBER_TITLE.match(text.strip()) and len(text.strip()) > 3:
            current = None                                      # 別の話題の見出しが来たら、課題の範囲は終わり
            last_role = "other"
            continue

        if action and _NOTE_HEAD.match(text):                   # ※ …グループ内で確認後、副手のチェックを受けること。
            nm = _NOTE_LABEL.search(text)
            claim = _claim(truncate_text(_flat(_NOTE_HEAD.sub("", text.strip(), count=1)), _MAX_LINE), b)
            if nm:
                num = _key("", nm.group(1), "")
                notes_by_num.setdefault(num, []).append((claim, b))
                for d in labeled:                                # 「【課題3】の調査終了後…」は課題3a・3bの両方に当てはまる
                    if _number_of(d.key) == num:
                        d.checks.append(claim)
            elif current is not None:
                current.checks.append(claim)
            last_role = "other"
            continue

        if action and _STEP_HEAD.match(text):                   # ①管理者権限に切り替える。
            lines = _split_steps(text)
            if current is not None:
                current.steps.extend(_claim(ln, b) for ln in lines)
            else:
                if group is None or n != last_step_index + 1:
                    group = _Draft("", False, b.page, n)
                    groups.append(group)
                    _title_and_commands_before(blocks, n, group)
                group.steps.extend(_claim(ln, b) for ln in lines)
            last_step_index = n
            last_role = "step"
            continue

        if action:                                              # 番号の付かない、作業を求める文
            if current is not None and len(current.do) < _MAX_DO:
                current.do.extend(_claim(ln, b) for ln in _bullets(text))
            last_role = "other"
            continue

        if _PROMPT.search(text):                                # コマンドの実行例
            if current is not None:
                current.add_commands(b)
            last_role = "cmd"
            continue

        # 課題の指示文の直後にある、条件の箇条書き
        if (current is not None and last_role in ("head", "cond") and n_cond < _MAX_COND and not b.is_heading
                and len(re.findall(rf"[{_CJK_CLASS}]", text)) >= 15 and not is_code_block(text)):
            current.conditions.extend(_claim(ln, b) for ln in _bullets(text))
            n_cond += 1
            last_role = "cond"
            continue
        last_role = "other"

    cards = [d for d in labeled]
    skipped: list[int] = []
    for g in groups:
        if len(g.steps) < 2:                                    # 番号つき手順だけの塊は、2つ以上あるときだけ
            continue
        if _directive_cue(blocks, g):
            cards.append(g)
        else:
            skipped.append(g.start_page)                        # 作業を求める言い方が無い手順の列挙は、載せない
    cards.sort(key=lambda d: d.order)
    if len(cards) > max_cards:
        keep = [d for d in cards if d.labeled][:max_cards]
        rest = [d for d in cards if not d.labeled][: max(0, max_cards - len(keep))]
        cards = sorted(keep + rest, key=lambda d: d.order)

    out: list[TaskCard] = []
    for d in cards:
        topic = ""
        if d.labeled:
            topic = topics.get(d.key) or topics.get(re.sub(r"[a-z]$", "", d.key), "")
        title = d.key if d.labeled else (d.title or "手順")
        out.append(TaskCard(title=title, topic=topic, labeled=d.labeled, place=d.place, do=d.do,
                            conditions=d.conditions, steps=d.steps, commands=d.commands, checks=d.checks))
    if not any(c.labeled for c in out):
        outline = None
    have = {_number_of(d.key) for d in labeled}
    missing = [k for k in dict.fromkeys(listed) if _number_of(k) not in have] if outline else []
    return TaskExtraction(out, outline, missing, sorted(set(skipped)))


def _title_and_commands_before(blocks: list[Block], n: int, group: _Draft) -> None:
    """番号つき手順の直前にある見出し（題）と、コマンド例を探す。"""
    cmd_blocks: list[Block] = []
    short = ""
    page = blocks[n].page
    for k in range(n - 1, max(-1, n - 9), -1):
        b = blocks[k]
        if b.page < page - 1:
            break
        if _PROMPT.search(b.text):
            if not short and not group.title:
                cmd_blocks.insert(0, b)             # 題より後ろ（手順の直前）にあるコマンド例だけを使う
            continue
        if is_code_block(b.text):
            continue
        t = _flat(b.text)
        if b.is_heading:
            group.title = t
            break
        if not short and len(t) <= 50 and "。" not in t:
            short = t
    if not group.title:
        group.title = short
    for b in cmd_blocks:
        group.add_commands(b)


_PAST = re.compile(r"(?:した|った|れた|いた|んだ|えた|けた|めた)[。．.]?$")
# 題が読者への呼びかけ・作業を表す語 / 実験・調査の説明を表す語
_READER_TITLE = re.compile(
    r"課題|演習|練習|実習|ワーク|やってみ|試してみ|意識して|心がけ|留意|気をつけ|やること|進め方|操作手順|作業手順|設定手順|"
    r"手順に従|やり方|使い方|セットアップ|インストール|して(?:ほしい|欲しい)|お願い"
)
_DESCRIBES = re.compile(r"実験|調査|研究|検証|分析|報告|事例|結果|実施")
_GOAL_HEAD = re.compile(r"目的|目標|ねらい|狙い|到達|成果")


def _directive_cue(blocks: list[Block], g: _Draft) -> bool:
    """番号つき手順の塊が、読者への作業指示と言える根拠（作業を求める文）を持つか。

    ①手順の文そのものが「〜しなさい／〜してください／〜すること」で終わる、②題が「課題・演習・意識して欲しいこと・
    操作手順」など読者への呼びかけや作業を表す（「実験・調査・研究」など説明の語を含まない）、③直前の2ブロックに
    作業を求める文がある。ただし、手順の半分以上が過去形（実験・調査を行った記録）なら、読者への指示ではない。
    """
    texts = [c.text for c in g.steps]
    if texts and sum(1 for t in texts if _PAST.search(re.sub(r"\s+", "", t))) * 2 >= len(texts):
        return False
    if any(is_directive_sentence(t) for t in texts):
        return True
    if _READER_TITLE.search(g.title or "") and not _DESCRIBES.search(g.title or ""):
        return True
    for k in range(max(0, g.order - 2), g.order):
        b = blocks[k]
        if b.page < g.start_page - 1 or b.is_heading:
            continue
        if any(is_directive_sentence(x) for x in _sentences(b.text)):
            return True
    return False


def _sentences(text: str) -> list[str]:
    """ブロックを1文ずつに分ける（句点と箇条書きの記号で区切る）。"""
    flat = re.sub(rf"[{_GLYPH}]", "\n", text)
    parts = re.split(r"(?<=[。．！!])|\n", flat)
    return [_flat(p) for p in parts if p and _flat(p)]


_MAX_INSTR = 10
_MAX_INSTR_CHARS = 200


def extract_instructions(blocks: list[Block], *, exclude_ids: set[int] | frozenset[int] = frozenset()) -> list[Claim]:
    """課題カード以外の場所にある、読者への作業指示の文を、原文のまま1文ずつ拾う。

    「〜しなさい」「〜してください」「〜しましょう」「〜すること」で終わる文だけが対象。見出し・コード例・
    学習目標（「目的は〜を理解すること」）は除く。モデルを使わないので、資料にない作業指示が混ざらない。
    """
    out: list[Claim] = []
    seen: set[str] = set()
    goal_section = False
    for b in blocks:
        if b.is_heading:
            goal_section = bool(_GOAL_HEAD.search(b.text))
            continue
        if goal_section or b.id in exclude_ids or is_code_block(b.text):
            continue
        for sent in _sentences(b.text):
            if not is_directive_sentence(sent):
                continue
            key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", sent))
            if key in seen:
                continue
            seen.add(key)
            out.append(_claim(truncate_text(sent, _MAX_INSTR_CHARS), b))
    return out[:_MAX_INSTR]
