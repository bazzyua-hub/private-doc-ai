"""セキュリティ要件（S-01〜S-04）のうち、静的に確認できるもの。"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SOURCES = sorted(APP.rglob("*.py"))

FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(urllib|requests|httpx|aiohttp|ftplib|smtplib|xmlrpc|websocket|websockets|telnetlib|openai|anthropic)\b",
    re.M,
)


def test_app_package_has_no_network_libraries():
    """S-01: アプリ本体は外部通信用のライブラリを使わない（通信は tools/setup_assets.py のみ）。"""
    bad = [p.name for p in SOURCES if FORBIDDEN_IMPORTS.search(p.read_text(encoding="utf-8"))]
    assert bad == []


def test_app_package_has_no_external_urls():
    for p in SOURCES:
        for url in re.findall(r"https?://[^\s\"')]+", p.read_text(encoding="utf-8")):
            assert url.startswith("http://127.0.0.1"), f"{p.name}: {url}"


def test_only_http_client_to_loopback_is_used_in_engine():
    src = (APP / "llm" / "engine.py").read_text(encoding="utf-8")
    assert "http.client" in src and "HTTPConnection(self.host" in src
    assert 'LOOPBACK = "127.0.0.1"' in (APP / "core" / "config.py").read_text(encoding="utf-8")


_WRITE = re.compile(r"open\([^)]*['\"][wa]b?\+?['\"]|\.write_text\(|\.write_bytes\(|tempfile|logging\.FileHandler|NamedTemporaryFile")


def test_no_document_content_is_written_to_disk_by_app():
    """S-02: app/ 内でファイルへ書き込む処理を持たない（ログ・一時ファイル・保存機能なし）。

    例外は2つだけ明示する:
    - app/gui/app.py: コンソールの無い起動（pythonw）で標準出力を捨てるための os.devnull
    - app/gui/main_window.py の save_to: 利用者が「保存」を押したときの書き出し（呼び出し元は次のテストで限定）
    """
    offenders = []
    for p in SOURCES:
        text = p.read_text(encoding="utf-8")
        if p.name == "app.py" and p.parent.name == "gui":
            text = re.sub(r"open\(os\.devnull[^)]*\)", "", text)        # devnull への出力は内容を残さない
        hits = _WRITE.findall(text)
        if p.name == "main_window.py" and p.parent.name == "gui":
            hits = [h for h in hits if h != ".write_text("] + [h for h in hits if h == ".write_text("][1:]   # 1箇所（save_to）だけ許可
        if hits:
            offenders.append(p.name)
    assert offenders == []


def test_gui_writes_a_file_only_from_the_explicit_save_action():
    """保存（save_to）は、利用者が押す「保存」ボタンの処理（save_result）からだけ呼ばれる。"""
    import ast
    tree = ast.parse((APP / "gui" / "main_window.py").read_text(encoding="utf-8"))
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "save_to":
                    callers.add(fn.name)
    assert callers == {"save_result"}
    # write_text は save_to の中だけ
    inside = [fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
              and any(isinstance(n, ast.Attribute) and n.attr == "write_text" for n in ast.walk(fn))]
    assert inside == ["save_to"]
    # PDF出力（write_pdf）も、save_to の中からだけ呼ぶ
    pdf_callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                   and any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "write_pdf" for n in ast.walk(fn))}
    assert pdf_callers == {"save_to"}
