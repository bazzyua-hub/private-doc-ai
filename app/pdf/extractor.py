"""PDFからのテキスト抽出（F-01 / F-02 / S-03）。

PyMuPDF（AGPL-3.0）への依存はこのファイルに閉じ込めてある。
将来 pypdfium2 などに差し替える場合は、`extract_pdf()` が返す ExtractResult を保てばよい。

- ページ番号とブロックIDを保持する（F-05 の土台）
- 画像データは読み込まない（TEXT_PRESERVE_IMAGES を外す）
- PDF内のスクリプトは実行されない（テキスト抽出のみ）
- 壊れた／暗号化／巨大なPDFは PdfError にする
"""
from __future__ import annotations

import contextlib
import time
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.core.errors import PdfError


@dataclass(frozen=True)
class Block:
    id: int                                   # 文書全体で一意（1始まり）
    page: int                                 # 1始まり
    text: str
    bbox: tuple[float, float, float, float]
    font_size: float
    page_height: float
    is_heading: bool = False

    @property
    def ref(self) -> str:
        return f"B{self.id}"


@dataclass
class ExtractResult:
    source_name: str
    page_count: int
    blocks: list[Block]
    total_chars: int
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


@dataclass
class _Raw:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float


_FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES


def _page_blocks(page) -> list[_Raw]:
    data = page.get_text("dict", flags=_FLAGS)
    raws: list[_Raw] = []
    for b in data.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines: list[str] = []
        weighted = 0.0
        weight = 0
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if text.strip():
                lines.append(text)
            for s in spans:
                n = len(s.get("text", "").strip())
                weighted += float(s.get("size", 0.0)) * n
                weight += n
        if not lines:
            continue
        x0, y0, x1, y1 = b["bbox"]
        raws.append(_Raw(x0, y0, x1, y1, "\n".join(lines), weighted / weight if weight else 0.0))
    return raws


def _reading_order(raws: list[_Raw], page_w: float) -> list[_Raw]:
    """2段組らしいページだけ「左段→右段」に並べ替える。それ以外は元の順序（内容ストリーム順）を保つ。"""
    if len(raws) < 4 or page_w <= 0:
        return raws
    mid = page_w / 2
    tol = 0.03 * page_w

    def kind(r: _Raw) -> str:
        if (r.x1 - r.x0) >= 0.6 * page_w:
            return "F"                       # 全幅（タイトル・図表キャプション等）
        if r.x1 <= mid + tol:
            return "L"
        if r.x0 >= mid - tol:
            return "R"
        return "F"

    kinds = [kind(r) for r in raws]
    n_left, n_right = kinds.count("L"), kinds.count("R")
    narrow_chars = sum(len(r.text) for r, k in zip(raws, kinds) if k in "LR")
    all_chars = sum(len(r.text) for r in raws) or 1
    if n_left < 2 or n_right < 2 or narrow_chars / all_chars < 0.6:
        return raws

    ordered: list[_Raw] = []
    left: list[_Raw] = []
    right: list[_Raw] = []

    def flush() -> None:
        ordered.extend(sorted(left, key=lambda r: r.y0))
        ordered.extend(sorted(right, key=lambda r: r.y0))
        left.clear()
        right.clear()

    for r in sorted(raws, key=lambda r: (r.y0, r.x0)):
        k = kind(r)
        if k == "F":
            flush()
            ordered.append(r)
        elif k == "L":
            left.append(r)
        else:
            right.append(r)
    flush()
    return ordered


_BULLET_START = re.compile(r"^\s*(?:[•・●○▪■□◆◇▶►\-–—*]|\d+[.)．）]\s|[（(]\d+[）)])")
_NUMBERED_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*[.．)）]?\s*\S")
_MERGE_MAX_CHARS = 800


def _merge_lines(raws: list[_Raw]) -> list[_Raw]:
    """1行ずつ別ブロックになっているPDF（Word/LibreOffice出力に多い）の行を、段落にまとめる。

    行間が通常の行送り程度・文字サイズが同じ・左端が近い・直前の行の真下、のときだけ結合する。
    箇条書きの先頭、短い番号つき見出し、表の別の列（縦に重なる）は結合しない。
    """
    out: list[_Raw] = []
    for r in raws:
        prev = out[-1] if out else None
        if prev is not None and _can_merge(prev, r):
            out[-1] = _Raw(min(prev.x0, r.x0), prev.y0, max(prev.x1, r.x1), r.y1,
                           prev.text + "\n" + r.text, prev.size)
        else:
            out.append(r)
    return out


def _can_merge(a: _Raw, b: _Raw) -> bool:
    size = a.size or b.size or 10.0
    if abs(a.size - b.size) > 0.6:
        return False
    gap = b.y0 - a.y1
    if gap < -1.0 or gap > 1.2 * size:
        return False                                    # 真下ではない／段落の切れ目
    if abs(b.x0 - a.x0) > 2.5 * size or b.x0 > a.x1 or b.x1 < a.x0:
        return False                                    # 別の列・別の位置
    if _BULLET_START.match(b.text):
        return False
    for t in (a.text, b.text):
        if len(t) < 40 and "\n" not in t and _NUMBERED_HEADING.match(t):
            return False                                # 「2. 目的と仮説」のような短い見出し
    return len(a.text) + len(b.text) <= _MERGE_MAX_CHARS


def extract_pdf(
    path: str | Path,
    *,
    max_mb: float = 200.0,
    max_pages: int = 500,
    max_chars_per_page: int = 50_000,
    low_text_chars: int = 10,
) -> ExtractResult:
    t0 = time.perf_counter()
    p = Path(path)
    if not p.is_file():
        raise PdfError(f"ファイルが見つかりません: {p.name}")
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise PdfError(f"ファイルが大きすぎます（{size_mb:.0f}MB。上限 {max_mb:.0f}MB）。")

    try:
        doc = pymupdf.open(str(p))
    except Exception as e:  # 破損・非PDF など。MuPDF側の例外型は多岐にわたる
        raise PdfError("PDFを開けませんでした。ファイルが壊れているか、PDF形式ではない可能性があります。") from e

    warnings: list[str] = []
    blocks: list[Block] = []
    with contextlib.closing(doc):
        if not doc.is_pdf:
            raise PdfError("PDF形式のファイルではありません。")
        if doc.needs_pass or doc.is_encrypted:
            raise PdfError("パスワード保護されたPDFには対応していません。")
        n_pages = doc.page_count
        if n_pages == 0:
            raise PdfError("ページが1枚もありません。")
        if n_pages > max_pages:
            raise PdfError(f"ページ数が多すぎます（{n_pages}ページ。上限 {max_pages}ページ）。")

        low_pages: list[int] = []
        failed_pages: list[int] = []
        total_chars = 0
        next_id = 1
        for idx in range(n_pages):
            pno = idx + 1
            try:
                page = doc[idx]
                raws = _page_blocks(page)
                page_w, page_h = page.rect.width, page.rect.height
            except Exception:
                failed_pages.append(pno)
                continue
            raws = _merge_lines(_reading_order(raws, page_w))
            page_chars = sum(len(r.text.strip()) for r in raws)
            total_chars += page_chars
            if page_chars < low_text_chars:
                low_pages.append(pno)

            budget = max_chars_per_page
            cut = False
            for r in raws:
                if budget <= 0:
                    cut = True
                    break
                text = r.text[:budget]
                cut = cut or len(text) < len(r.text)
                budget -= len(text)
                blocks.append(
                    Block(
                        id=next_id,
                        page=pno,
                        text=text,
                        bbox=(r.x0, r.y0, r.x1, r.y1),
                        font_size=r.size,
                        page_height=page_h,
                    )
                )
                next_id += 1
            if cut:
                warnings.append(f"p.{pno} は文字数が多すぎるため途中で打ち切りました。")

    if failed_pages:
        warnings.append(f"{_pages(failed_pages)} は読み取りに失敗したため除外しました。")
    if total_chars == 0:
        raise PdfError(
            "文字情報を抽出できませんでした。スキャンPDFや画像主体のPDFの可能性があります"
            "（OCRには対応していません）。"
        )
    if len(low_pages) >= max(1, n_pages // 2):
        warnings.append(
            "半数以上のページで文字がほとんど抽出できませんでした。画像主体のPDFの可能性があり、"
            "要約が不完全になる恐れがあります。"
        )
    elif low_pages:
        warnings.append(f"{_pages(low_pages)} は文字がほとんど抽出できませんでした（画像のみのページの可能性）。")

    return ExtractResult(
        source_name=p.name,
        page_count=n_pages,
        blocks=blocks,
        total_chars=total_chars,
        warnings=warnings,
        elapsed_s=time.perf_counter() - t0,
    )


def _pages(pages: list[int], limit: int = 10) -> str:
    shown = ", ".join(f"p.{n}" for n in pages[:limit])
    return shown + (f" ほか{len(pages) - limit}ページ" if len(pages) > limit else "")
