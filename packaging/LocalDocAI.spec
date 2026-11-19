# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller の設定（onedir: LocalDocAI.exe と _internal フォルダになる）。

ビルド:  python tools/build_package.py   （内部でこのファイルを使う）
直接:    python -m PyInstaller packaging/LocalDocAI.spec --noconfirm --clean

- 画面を持つアプリなので console=False（黒い窓を出さない）。ログは出さない。
- onedir にする理由: ライブラリのファイルが分かれて残るので、LGPL（PySide6/Qt）の「差し替え可能」の条件を満たしやすく、
  起動も onefile より速い（onefile は毎回一時フォルダへ展開する。文書の処理で一時ファイルを作らない方針にも合う）。
- 使わない Qt の部品（ネットワーク・Web・QML・マルチメディアなど）は入れない。サイズが減り、「通信しない」の裏付けにもなる。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent                      # noqa: F821  (PyInstaller が SPECPATH を渡す)
ICON = ROOT / "packaging" / "LocalDocAI.ico"

EXCLUDES = [
    # Qt の使わない部品
    "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtDBus", "PySide6.QtXml", "PySide6.QtPrintSupport", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtBluetooth",
    # Python の使わない標準部品・開発用
    "tkinter", "unittest", "pydoc", "doctest", "pytest", "_pytest", "setuptools", "pip", "distutils", "PIL", "numpy",
    "http.server", "xmlrpc", "ftplib", "smtplib", "imaplib", "poplib", "telnetlib",
]

a = Analysis(                                                # noqa: F821
    [str(ROOT / "gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "app" / "gui" / "assets" / "fonts"), "app/gui/assets/fonts")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)                                            # noqa: F821

exe = EXE(                                                   # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalDocAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                               # UPX は誤検知（ウイルス判定）の原因になりやすいので使わない
    console=False,
    icon=str(ICON) if ICON.exists() else None,
)
coll = COLLECT(                                              # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LocalDocAI",
)
