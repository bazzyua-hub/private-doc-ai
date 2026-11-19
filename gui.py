"""LocalDoc AI — Phase 3: GUI版の起動。 使い方: python gui.py [資料.pdf]

配布版の起動確認: LocalDocAI.exe --selftest 結果.txt   （画面は出さず、必要な部品が使えるかを調べて、報告を書き出す）
"""
import sys


def run_selftest(args: list[str]) -> int:
    from app.gui.app import selftest

    code, text = selftest()
    out = args[args.index("--selftest") + 1] if args.index("--selftest") + 1 < len(args) else None
    if out:
        with open(out, "w", encoding="utf-8") as f:                 # 報告（部品の有無）だけ。文書の内容は含まない
            f.write(text)
    elif sys.stdout is not None:
        sys.stdout.write(text)
    return code


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest(sys.argv))
    from app.gui.app import main

    sys.exit(main())
