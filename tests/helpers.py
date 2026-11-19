"""テスト用のPDF生成・ブロック生成・偽エンジン。"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from app.core.errors import Cancelled
from app.llm.engine import ChatResult
from app.pdf.extractor import Block


def make_pdf(path: Path, pages: list[list[str]], *, header: str | None = None, footer: bool = False, jp: bool = True) -> Path:
    """各段落を縦に離して配置したPDFを作る。段落が "# " で始まれば大きい文字（見出し）にする。"""
    font = "japan" if jp else "helv"
    doc = pymupdf.open()
    for n, paras in enumerate(pages, start=1):
        page = doc.new_page()
        if header:
            page.insert_text((50, 30), header.format(n=n), fontname=font, fontsize=9)
        y = 90.0
        for p in paras:
            heading = p.startswith("# ")
            text = p[2:] if heading else p
            size = 18 if heading else 11
            h = 30 if heading else 70
            page.insert_textbox(pymupdf.Rect(50, y, 545, y + h), text, fontname=font, fontsize=size)
            y += h + 25
        if footer:
            page.insert_text((270, 815), f"- {n} -", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def make_two_column_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 70), "A Study of Relational Databases", fontname="helv", fontsize=18)
    cols = {
        "L": [("Left one. " * 8), ("Left two. " * 8), ("Left three. " * 8)],
        "R": [("Right one. " * 8), ("Right two. " * 8), ("Right three. " * 8)],
    }
    # 一般的な論文PDFと同じく、内容ストリームでは左段を全部出してから右段を出す
    for side, x0, x1 in (("L", 50, 290), ("R", 310, 545)):
        for i in range(3):
            y = 120 + i * 110
            page.insert_textbox(pymupdf.Rect(x0, y, x1, y + 90), cols[side][i], fontname="helv", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def blk(i: int, page: int, text: str, *, size: float = 10.0, y0: float = 100.0, heading: bool = False,
        page_h: float = 842.0) -> Block:
    return Block(id=i, page=page, text=text, bbox=(50.0, y0, 500.0, y0 + 12), font_size=size,
                 page_height=page_h, is_heading=heading)


class FakeEngine:
    """LLM を使わずに要約ロジックをテストするための偽エンジン。responder(system, user, n) が出力を決める。"""

    def __init__(self, responder, on_call=None) -> None:
        self.responder = responder
        self.on_call = on_call
        self.calls: list[tuple[str, str, int]] = []

    def chat(self, system, user, *, max_tokens, cancel=None) -> ChatResult:
        if cancel is not None:
            cancel.raise_if_set()
        self.calls.append((system, user, max_tokens))
        if self.on_call:
            self.on_call(len(self.calls))
        out = self.responder(system, user, len(self.calls))
        if isinstance(out, Exception):
            raise out
        finish = "stop"
        if isinstance(out, tuple):                    # (text, finish_reason) で出力の途中切れを再現できる
            out, finish = out
        return ChatResult(text=out, prompt_tokens=100, prompt_eval_tokens=100, completion_tokens=50,
                          prompt_s=1.0, gen_s=2.0, finish_reason=finish)


def ids_in(user: str, prefix: str = "B") -> list[int]:
    return [int(x) for x in re.findall(rf"^\[{prefix}(\d+)\]", user, re.M)]


def is_map(user: str) -> bool:
    """詳細モードの map 用プロンプトか（部分要約の本文に紛れない固有の文言で判定）。"""
    return "この部分の要点（事実・説明・結論・数値・条件）" in user


def quotes(user: str, n: int | None = None, prefix: str = "B", width: int = 60) -> list[tuple[int, str]]:
    """プロンプトに載っている原文ブロックから、(番号, 先頭の width 文字) を取り出す。
    「原文に基づく（根拠のある）モデル出力」を偽エンジンに作らせるための道具。"""
    out = [(int(m.group(1)), m.group(2).strip()[:width]) for m in re.finditer(rf"^\[{prefix}(\d+)\]\s*(.*)$", user, re.M)]
    return out if n is None else out[:n]


def grounded_lines(user: str, n: int = 1, *, width: int = 60) -> str:
    """原文を引用した要点の行（- 原文 [B番号]）。map の応答用。"""
    return "".join(f"- {t} [B{i}]\n" for i, t in quotes(user, n, width=width))
