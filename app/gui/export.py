"""PDFへの書き出し（利用者が「保存」を押したときだけ、main_window.save_to から呼ばれる）。

追加のライブラリは使わず、Qt の QTextDocument → QPdfWriter で出力する。
画面と同じ見た目（薄ピンク・丸ゴシック）で、絵文字は使わない版のHTMLを描く（絵文字は環境によってPDFに載らないため）。
フォントはPDFに埋め込まれるので、受け取った人のPCにフォントが無くても同じ見た目で開ける。
"""
from __future__ import annotations

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

from app.summary.schemas import SummaryResult

from .html import Colors, esc, result_to_html


def pdf_html(result: SummaryResult, colors: Colors, *, show_sources: bool = False) -> str:
    title = result.source_name or "要約"
    head = (f'<h1 style="font-family:\'{colors.pop_family}\'; font-weight:normal; color:{colors.accent_dark};'
            f' font-size:20pt; margin-bottom:2px;">{esc(title)}</h1>'
            f'<p style="color:{colors.muted}; font-size:9pt; margin-top:0;">'
            f'LocalDoc AI による要約（{result.page_count}ページ）　※AIの要約です。大事な点は原文で確認してください</p>')
    return head + result_to_html(result, colors, show_sources=show_sources, icons=False)


def write_pdf(html: str, path: str, colors: Colors) -> None:
    """HTMLをA4のPDFとして path へ書き出す。失敗は例外（OSError など）で伝える。"""
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(16, 16, 16, 16), QPageLayout.Unit.Millimeter)
    writer.setTitle("LocalDoc AI 要約")
    writer.setCreator("LocalDoc AI")
    doc = QTextDocument()
    font = QFont(colors.body_family)
    font.setPointSizeF(10)
    doc.setDefaultFont(font)
    doc.setHtml(html)
    doc.print_(writer)
