"""GUIの起動。"""
from __future__ import annotations

import os
import sys
import traceback


def selftest() -> tuple[int, str]:
    """配布版の起動確認（`LocalDocAI.exe --selftest 結果.txt`）。画面は出さず、必要な部品が使えるかを確かめる。

    文書は読まない。ファイルも作らない（PDFはメモリ上で作るだけ）。報告の文章を返し、書き出しは起動口（gui.py）が行う。
    llama-server とモデルの有無は報告するだけで、失敗にはしない（zip に同梱しない配布もあるため）。
    """
    import io

    lines: list[str] = []
    ok = True

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        lines.append(f"{'OK ' if good else 'NG '} {name}" + (f"  {detail}" if detail else ""))

    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication

    from app.core.config import APP_ROOT, APP_VERSION, Config, is_frozen, resolve_model, resolve_server
    from app.core.errors import AssetNotFoundError

    app = QApplication.instance() or QApplication(["selftest"])
    lines.append(f"LocalDoc AI {APP_VERSION}  frozen={is_frozen()}  root={APP_ROOT}")
    try:
        from .theme import apply_theme, load_fonts
        colors = apply_theme(app)
        fonts = load_fonts()
        check("同梱フォントの読み込み（丸ポップ・丸ゴシック）", fonts.loaded and fonts.pop != fonts.body, f"{fonts.pop} / {fonts.body}")
    except Exception as e:  # noqa: BLE001
        check("フォント", False, repr(e))
        colors = None
    try:
        import pymupdf
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "selftest")
        data = doc.tobytes()
        text = pymupdf.open(stream=io.BytesIO(data), filetype="pdf")[0].get_text()
        check("PDF読み取り（PyMuPDF）", "selftest" in text)
    except Exception as e:  # noqa: BLE001
        check("PDF読み取り（PyMuPDF）", False, repr(e))
    try:
        from PySide6.QtGui import QPdfWriter, QTextDocument
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        w = QPdfWriter(buf)
        d = QTextDocument()
        d.setHtml("<p>要約</p>")
        d.print_(w)
        check("PDF書き出し（Qt）", buf.size() > 500, f"{buf.size()} bytes")
    except Exception as e:  # noqa: BLE001
        check("PDF書き出し（Qt）", False, repr(e))
    try:
        from PySide6.QtCore import QSettings

        from .main_window import MainWindow
        # 利用者の設定を書き換えないよう、別名の設定を読むだけにする（設定の書き込みは closeEvent なので、閉じずに破棄する）
        w = MainWindow(settings=QSettings("LocalDocAI-selftest", "selftest"), show_dialogs=False)
        w.deleteLater()
        check("画面の組み立て", True)
    except Exception as e:  # noqa: BLE001
        check("画面の組み立て", False, repr(e))
    cfg = Config()
    for label, fn in (("llama-server", resolve_server), ("モデル(.gguf)", resolve_model)):
        try:
            lines.append(f"-- {label}: {fn(cfg)}")
        except AssetNotFoundError:
            lines.append(f"-- {label}: 見つかりません（zipに同梱していない場合は正常です）")
    lines.append("RESULT " + ("OK" if ok else "NG"))
    return (0 if ok else 1), "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if sys.stderr is None:                                 # pythonw.exe（コンソール無し）で起動された場合
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    from PySide6.QtWidgets import QApplication, QMessageBox

    from .main_window import APP_TITLE, MainWindow
    from .theme import apply_theme

    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("LocalDocAI")
    apply_theme(app)                                       # 薄ピンクのテーマ・同梱フォント・アイコン

    def excepthook(etype, value, tb) -> None:
        traceback.print_exception(etype, value, tb, file=sys.stderr)
        QMessageBox.critical(None, APP_TITLE, f"予期しないエラーが起きました（{etype.__name__}）。\nアプリを再起動してください。")

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    pdfs = [a for a in argv[1:] if a.lower().endswith(".pdf")]
    if pdfs:
        win.load_pdf(pdfs[0])
    return app.exec()
