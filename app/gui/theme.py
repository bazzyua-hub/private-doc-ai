"""見た目（テーマ）: 薄ピンクを基調にした、丸ゴシック＋丸ポップ体のデザイン。

- フォントはアプリに同梱（app/gui/assets/fonts、SIL OFL）。Windows に丸ポップ系の標準フォントが無いため。
  読み込めなければ、システムの日本語フォントで表示する（見た目が変わるだけで動作は変わらない）
- 色は html.Colors が持ち、QSS とHTML表示の両方がそれを使う（色の定義は1か所）
- 利用者のOSが暗いテーマでも、アプリは明るいピンクのまま表示する
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QLinearGradient, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

from .html import Colors

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
BODY_FILES = ("ZenMaruGothic-Regular.ttf", "ZenMaruGothic-Bold.ttf")
POP_FILES = ("HachiMaruPop-Regular.ttf",)
FALLBACKS = ("Yu Gothic UI", "Meiryo UI", "Noto Sans CJK JP", "sans-serif")

PINK = Colors()


@dataclass(frozen=True)
class Fonts:
    body: str
    pop: str
    loaded: bool                 # 同梱フォントを読み込めたか


_fonts: Fonts | None = None


def _register(files: tuple[str, ...]) -> str:
    family = ""
    for name in files:
        fid = QFontDatabase.addApplicationFont(str(FONT_DIR / name))
        if fid >= 0 and not family:
            fams = QFontDatabase.applicationFontFamilies(fid)
            family = next((f for f in fams if f.isascii()), fams[0] if fams else "")
    return family


def load_fonts() -> Fonts:
    """同梱フォントを登録する（QApplication の生成後に呼ぶ。2回目以降は同じ結果を返す）。"""
    global _fonts
    if _fonts is None:
        body, pop = _register(BODY_FILES), _register(POP_FILES)
        _fonts = Fonts(body or FALLBACKS[0], pop or body or FALLBACKS[0], bool(body and pop))
    return _fonts


def families(primary: str) -> str:
    """QSS の font-family 指定（同梱フォントが無いときのために、システムの日本語フォントを続ける）。"""
    return ", ".join(f'"{f}"' if " " in f else f for f in (primary, *FALLBACKS))


def theme_colors() -> Colors:
    f = load_fonts()
    from dataclasses import replace
    return replace(PINK, body_family=f.body, pop_family=f.pop)


def build_stylesheet(c: Colors) -> str:
    body, pop = families(c.body_family), families(c.pop_family)
    return f"""
    QWidget {{ font-family: {body}; font-size: 10.5pt; color: {c.text}; }}
    QMainWindow, QWidget#root {{ background: {c.bg}; }}
    QToolTip {{ background: {c.surface}; color: {c.text}; border: 1px solid {c.border}; padding: 4px; }}
    QMessageBox, QDialog {{ background: {c.bg}; }}

    QFrame#header {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FFD3E4, stop:1 {c.soft});
                     border-radius: 22px; }}
    QLabel {{ background: transparent; }}
    QLabel#title {{ font-family: {pop}; font-size: 26pt; color: {c.accent_dark}; }}
    QLabel#tagline {{ font-family: {pop}; font-size: 11pt; color: {c.accent}; }}
    QLabel#pill {{ background: {c.surface}; color: {c.muted}; border-radius: 12px; padding: 4px 12px; font-size: 9pt; }}
    QLabel#muted {{ color: {c.muted}; }}
    QLabel#error {{ color: {c.warn}; background: {c.warn_bg}; border-radius: 12px; padding: 8px 12px; }}
    QLabel#status {{ font-size: 11pt; color: {c.text}; }}

    QFrame#drop {{ background: {c.surface}; border: 3px dashed {c.border}; border-radius: 22px; }}
    QFrame#drop[dragging="true"] {{ background: {c.soft}; border: 3px dashed {c.accent}; }}
    QFrame#panel {{ background: {c.surface}; border: 2px solid {c.border}; border-radius: 22px; }}
    QFrame#panel QLabel, QFrame#drop QLabel {{ background: transparent; }}

    QPushButton {{ background: {c.surface}; color: {c.accent_dark}; border: 2px solid {c.border}; border-radius: 16px;
                   padding: 6px 18px; }}
    QPushButton:hover {{ background: {c.soft}; border-color: {c.accent_hi}; }}
    QPushButton:pressed {{ background: #FFD3E4; }}
    QPushButton:disabled {{ color: #D9B9C8; border-color: #F8E1EA; background: #FFFAFC; }}
    QPushButton#primary {{ font-family: {pop}; font-size: 15pt; color: #FFFFFF; border: none; border-radius: 24px;
                           padding: 8px 30px;
                           background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c.accent_hi}, stop:1 {c.accent}); }}
    QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFA3C3, stop:1 #F47DA7); }}
    QPushButton#primary:pressed {{ background: {c.accent}; }}
    QPushButton#primary:disabled {{ background: #F8D3E1; color: #FFFFFF; }}
    QPushButton#primary[stop="true"] {{ background: {c.surface}; color: {c.warn}; border: 2px solid #F4B79B; }}
    QPushButton#primary[stop="true"]:hover {{ background: {c.warn_bg}; }}
    QToolButton {{ background: transparent; color: {c.muted}; border: none; padding: 4px 8px; border-radius: 10px; }}
    QToolButton:hover {{ background: {c.soft}; }}

    QRadioButton, QCheckBox {{ spacing: 8px; background: transparent; }}
    QRadioButton:disabled, QCheckBox:disabled {{ color: #C9A9B9; }}
    QRadioButton::indicator {{ width: 18px; height: 18px; border-radius: 11px; border: 2px solid {c.accent_hi};
                               background: {c.surface}; }}
    QRadioButton::indicator:checked {{ border: 2px solid {c.accent};
        background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                    stop:0 {c.accent}, stop:0.5 {c.accent}, stop:0.56 {c.surface}, stop:1 {c.surface}); }}
    QRadioButton::indicator:disabled {{ border-color: #EBCBD9; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 6px; border: 2px solid {c.accent_hi};
                            background: {c.surface}; }}
    QCheckBox::indicator:checked {{ background: {c.accent}; border-color: {c.accent}; }}
    QCheckBox::indicator:disabled {{ border-color: #EBCBD9; }}

    QProgressBar {{ border: none; background: {c.soft}; border-radius: 7px; }}
    QProgressBar::chunk {{ border-radius: 7px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c.accent_hi}, stop:1 {c.accent}); }}

    QTextBrowser {{ background: {c.surface}; border: 2px solid {c.border}; border-radius: 22px; padding: 12px 16px;
                    selection-background-color: {c.soft}; selection-color: {c.text}; }}
    QScrollBar:vertical {{ background: transparent; width: 14px; margin: 14px 3px 14px 0; }}
    QScrollBar::handle:vertical {{ background: {c.border}; border-radius: 5px; min-height: 36px; }}
    QScrollBar::handle:vertical:hover {{ background: {c.accent_hi}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QStatusBar {{ background: transparent; color: {c.muted}; }}
    """


def light_palette(c: Colors) -> QPalette:
    """OSが暗いテーマでも、明るいピンクのまま表示するためのパレット。"""
    p = QPalette()
    for role, color in ((QPalette.ColorRole.Window, c.bg), (QPalette.ColorRole.Base, c.surface),
                        (QPalette.ColorRole.AlternateBase, c.card_bg), (QPalette.ColorRole.Button, c.surface),
                        (QPalette.ColorRole.Text, c.text), (QPalette.ColorRole.WindowText, c.text),
                        (QPalette.ColorRole.ButtonText, c.accent_dark), (QPalette.ColorRole.ToolTipBase, c.surface),
                        (QPalette.ColorRole.ToolTipText, c.text), (QPalette.ColorRole.Highlight, c.accent),
                        (QPalette.ColorRole.HighlightedText, "#FFFFFF"), (QPalette.ColorRole.PlaceholderText, c.muted)):
        p.setColor(role, QColor(color))
    return p


def make_icon(c: Colors | None = None) -> QIcon:
    """アプリのアイコン（ピンクの丸に「要」）。画像ファイルを持たず、起動時に描く。"""
    c = c or theme_colors()
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, size)
        grad.setColorAt(0, QColor(c.accent_hi))
        grad.setColorAt(1, QColor(c.accent))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(size * 0.04), int(size * 0.04), int(size * 0.92), int(size * 0.92))
        p.setPen(QColor("#FFFFFF"))
        f = QFont(c.pop_family)
        f.setPixelSize(int(size * 0.58))
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "要")
        p.end()
        icon.addPixmap(pm)
    return icon


def apply_theme(app: QApplication) -> Colors:
    """アプリ全体にテーマを適用する（QApplication の生成直後に1回）。"""
    app.setStyle("Fusion")                                   # スタイルシートが確実に効く、描画の癖が少ないスタイル
    c = theme_colors()
    f = QFont(c.body_family)
    f.setPointSizeF(10.5)
    app.setFont(f)
    app.setPalette(light_palette(c))
    app.setStyleSheet(build_stylesheet(c))
    app.setWindowIcon(make_icon(c))
    return c
