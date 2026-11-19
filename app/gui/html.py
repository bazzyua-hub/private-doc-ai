"""要約結果 → 画面表示用のHTML（Qt のリッチテキストで描ける範囲）。

PDFの本文は信頼できない入力なので、**すべてエスケープして**埋め込む（タグや画像参照として解釈させない）。
配色は Colors（薄ピンクのテーマ）。出典・状態は「チップ」（色つきの小さなラベル）で見せる。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

from app.summary.schemas import STATUS_UNRESOLVED, Claim, SummaryResult, TaskCard, format_pages


@dataclass(frozen=True)
class Colors:
    """画面全体の配色とフォント。QSS（theme.py）と結果のHTMLの両方がこれを使う。"""
    text: str = "#5A3A49"          # 本文（黒ではなく、少し赤みのある暗い色）
    muted: str = "#8E6477"         # 補足
    bg: str = "#FFF3F7"            # 全体の背景（薄ピンク）
    surface: str = "#FFFFFF"       # カード・結果欄の背景
    border: str = "#F6C1D6"        # 枠線
    accent: str = "#F06C9B"        # 主役のピンク
    accent_hi: str = "#FF8DB3"     # 明るいピンク（グラデーション・ホバー）
    accent_dark: str = "#C2457F"   # 見出し・強調の濃いピンク
    soft: str = "#FFE3EE"          # チップ・ヘッダーの薄いピンク
    card_bg: str = "#FFF8FB"       # 課題カードの背景
    code_bg: str = "#FFEEF4"       # コマンドの背景
    warn: str = "#C2410C"          # 未特定・警告（ピンクの中でも目立つ珊瑚色）
    warn_bg: str = "#FFEBDD"
    body_family: str = "Zen Maru Gothic"
    pop_family: str = "Hachi Maru Pop"


_LABEL_W = 92                  # 課題カードの見出し列の幅（px）。コマンド欄の長さに引きずられて広がらないように固定する

SECTION_ICONS = {
    "概要": "🌼", "主要ポイント": "💡", "詳細": "📖", "注意点・限界": "🔔", "やること・手順": "📝",
    "処理上の注意": "🔔", "参照した原文": "📎",
}


def esc(text: str) -> str:
    return _html.escape(text, quote=True)


_BREAKABLE = re.compile(r"([/\\._\-?=:,])(?=[^\s\u200b])")


def wrap_esc(text: str) -> str:
    """エスケープして、URL・パスのような長い文字列の途中にも「改行してよい位置」(ゼロ幅スペース)を入れる。

    入れないと、改行できない長い文字列が表の幅を押し広げて、枠からはみ出す。表示専用（コピー・保存の文には入れない）。
    """
    return _BREAKABLE.sub("\\1\u200b", esc(text))


def _chip(text: str, fg: str, bg: str, size: str = "small") -> str:
    """色つきの小さなラベル（出典・状態）。"""
    return f'<span style="background-color:{bg}; color:{fg}; font-size:{size};">&nbsp;{esc(text)}&nbsp;</span>' if text else ""


def _strip_paren(note: str) -> str:
    return note.strip().removeprefix("（").removesuffix("）")


def _notes(result: SummaryResult, c: Claim, colors: Colors) -> str:
    note, warn = result.claim_notes(c)
    if c.status == STATUS_UNRESOLVED:
        chip = _chip(_strip_paren(note), colors.warn, colors.warn_bg)
    else:
        chip = _chip(_strip_paren(note), colors.accent_dark, colors.soft)
    return chip + (" " + _chip("⚠" + warn.strip().removeprefix("⚠"), colors.warn, colors.warn_bg) if warn else "")


def _h2(title: str, colors: Colors, extra: str = "", icons: bool = True) -> str:
    icon = (SECTION_ICONS.get(title, "🌸") + " ") if icons else ""
    return (f'<h2 style="font-family:\'{colors.pop_family}\'; font-weight:normal; color:{colors.accent_dark};'
            f' font-size:15pt; margin-top:20px; margin-bottom:6px;">{icon}{esc(title)}{extra}</h2>')


def _claim_li(result: SummaryResult, c: Claim, colors: Colors) -> str:
    return f"<li style=\"margin-bottom:5px;\">{wrap_esc(c.text)} {_notes(result, c, colors)}</li>"


def _card_html(result: SummaryResult, card: TaskCard, colors: Colors, icons: bool = True) -> str:
    pages = result.pages_of(card.block_ids())
    multi = len(pages) > 1
    key = _chip(card.title, "#FFFFFF", colors.accent, "10pt") if card.labeled else ("📌" if icons else "■")
    topic = f"　{esc(card.topic)}" if card.topic else ("" if card.labeled else f" {esc(card.title)}")
    page_chip = f"　{_chip(format_pages(pages), colors.accent_dark, colors.soft)}" if pages else ""
    head = (f'<p style="font-family:\'{colors.pop_family}\'; font-size:12.5pt; color:{colors.text}; margin-top:14px;'
            f' margin-bottom:4px;">{key}{topic}{page_chip}</p>')
    rows: list[str] = []

    def pg(c: Claim) -> str:
        ps = result.pages_of(c.block_ids)
        return f" {_chip(format_pages(ps), colors.muted, colors.soft)}" if multi and ps else ""

    for label, claims in card.lines():
        if not claims:
            continue
        if label == "コマンド":
            where = f'<span style="color:{colors.muted}; font-size:small;">{esc(card.place)}</span>' if card.place else ""
            body = (where + f'<pre style="background-color:{colors.code_bg}; margin:0; padding:5px; white-space:pre-wrap;">'
                    + wrap_esc("\n".join(c.text for c in claims)) + "</pre>")
        elif label == "手順":
            body = "<br>".join(wrap_esc(c.text) + pg(c) for c in claims)
        elif label == "条件" or len(claims) > 1:
            body = "<br>".join("・" + wrap_esc(c.text) + pg(c) for c in claims)
        else:
            body = wrap_esc(claims[0].text) + pg(claims[0])
        rows.append(f'<tr><td width="{_LABEL_W}" valign="top">{_chip(label, colors.accent_dark, colors.soft, '9.5pt')}</td>'
                    f'<td valign="top" style="padding-bottom:7px;">{body}</td></tr>')
    return (head + f'<table width="100%" cellspacing="0" cellpadding="6" border="1" bordercolor="{colors.border}"'
            f' style="background-color:{colors.card_bg}; border-color:{colors.border};">'
            f'<tr><td><table width="100%" cellspacing="0" cellpadding="2">{"".join(rows)}</table></td></tr></table>')


def result_to_html(result: SummaryResult, colors: Colors | None = None, *, show_sources: bool = False,
                   snippet_chars: int = 200, icons: bool = True) -> str:
    """icons=False は絵文字を使わない版（PDF出力用。絵文字は環境によってPDFに載らないことがあるため）。"""
    colors = colors or Colors()
    out: list[str] = []
    for sec in result.sections:
        out.append(_h2(sec.title, colors, icons=icons))
        if not sec.claims:
            out.append(f'<p style="color:{colors.warn};">（該当なし・モデルから有効な出力が得られませんでした）</p>')
            continue
        if sec.title == "概要" and len(sec.claims) == 1:
            c = sec.claims[0]
            out.append(f"<p>{esc(c.text)} {_notes(result, c, colors)}</p>")
        else:
            out.append("<ul>" + "".join(_claim_li(result, c, colors) for c in sec.claims) + "</ul>")

    if result.tasks or result.instructions:
        out.append(_h2("やること・手順", colors, icons=icons, extra=
                       f' <span style="font-family:\'{colors.body_family}\'; color:{colors.muted}; font-size:small;">'
                       "（原文から抜き出し）</span>"))
        if result.task_outline:
            pages = format_pages(result.pages_of(result.task_outline.block_ids))
            out.append(f"<p>{_chip('全体', '#FFFFFF', colors.accent_dark)} {wrap_esc(result.task_outline.text)} "
                       f"{_chip(pages, colors.accent_dark, colors.soft)}</p>")
        out.extend(_card_html(result, card, colors, icons) for card in result.tasks)
        if result.instructions:
            label = "その他の、作業を求める文" if result.tasks else "作業を求める文"
            items = "".join(
                f"<li style=\"margin-bottom:5px;\">{wrap_esc(c.text)} "
                f"{_chip(format_pages(result.pages_of(c.block_ids)), colors.accent_dark, colors.soft)}</li>"
                for c in result.instructions)
            out.append(f"<p style=\"margin-top:12px;\">{_chip(label, colors.accent_dark, colors.soft, '9.5pt')}</p><ul>{items}</ul>")
    else:
        out.append(_h2("やること・手順", colors, icons=icons))
        out.append(f'<p style="color:{colors.muted};">{esc(result.NO_TASKS)}</p>')

    if show_sources:
        rows = [f"<li>{_chip(f'p.{page}', colors.accent_dark, colors.soft)} {wrap_esc(text)}</li>"
                for _, page, text in result.evidence_snippets(snippet_chars)]
        if rows:
            out.append(_h2("参照した原文", colors, icons=icons) + "<ul>" + "".join(rows) + "</ul>")

    if result.warnings:
        out.append(_h2("処理上の注意", colors, icons=icons) + "<ul>"
                   + "".join(f'<li style="color:{colors.muted};">{esc(w)}</li>' for w in result.warnings) + "</ul>")
    return "".join(out)
