"""アプリのアイコン（theme.make_icon が描く、ピンクの丸に「要」）を packaging/LocalDocAI.ico として書き出す。

配布版の exe のアイコン用。見た目を変えたときだけ実行して、できた .ico をコミットする。
    python tools/make_icon.py
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.gui.theme import apply_theme, make_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """PNG画像の列から .ico を組み立てる（Vista 以降は PNG 埋め込みの ico を読める）。"""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries, data, offset = b"", b"", 6 + 16 * len(pngs)
    for size, png in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    return head + entries + data


def main() -> int:
    app = QApplication.instance() or QApplication(["make_icon"])
    apply_theme(app)
    icon = make_icon()
    pngs: list[tuple[int, bytes]] = []
    for s in SIZES:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(s, s).save(buf, "PNG")
        pngs.append((s, bytes(buf.data())))
    out = Path(__file__).resolve().parents[1] / "packaging" / "LocalDocAI.ico"
    out.write_bytes(build_ico(pngs))
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {SIZES})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
