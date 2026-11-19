"""GUI（PySide6）のテスト。画面を出さない offscreen で動かす。PySide6 が無い環境では飛ばす。"""
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QSettings, QUrl                    # noqa: E402
from PySide6.QtWidgets import QApplication                                 # noqa: E402

from app.core.config import Config                                         # noqa: E402
from app.core.errors import AssetNotFoundError                             # noqa: E402
from app.gui.html import Colors, result_to_html                            # noqa: E402
from app.gui.main_window import MainWindow, format_elapsed, format_estimate  # noqa: E402
from app.gui.worker import stage_fraction                                  # noqa: E402
from app.pipeline import estimate_seconds, prepare                         # noqa: E402
from app.summary.schemas import Claim, Section, SummaryResult              # noqa: E402
from app.summary.tasks import extract_task_cards                           # noqa: E402

from helpers import FakeEngine, ids_in, is_map, make_pdf                    # noqa: E402
from test_tasks import _doc                                                # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(qapp, cond, timeout=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert cond(), "時間内に条件が満たされませんでした"


class Ctx:
    """with 文で使える偽エンジン（停止が呼ばれたかを記録する）。"""
    startup_s = 1.5
    peak_rss_mb = 1234.0

    def __init__(self, responder):
        self.engine = FakeEngine(responder)
        self.stopped = False

    def __enter__(self):
        return self.engine

    def __exit__(self, *exc):
        self.stopped = True


def fast_responder(system, user, n):
    ids = ids_in(user)
    return ("## 概要\n- 資料はデータベースの正規化を説明している。\n\n## 主要ポイント\n"
            f"- 正規化は冗長性を減らし整合性を保つ [B{ids[0]}]\n")


JP = "データベースの正規化では冗長性を減らして整合性を保つことが重要である。" * 3


def pdf(tmp_path, pages=2):
    """ページごとに内容の違う、文字のあるPDF（1ページ3段落）。"""
    return str(make_pdf(tmp_path / "資料.pdf",
                        [[f"第{i}章{j}節　{JP}" for j in range(1, 4)] for i in range(1, pages + 1)]))


@pytest.fixture
def window(qapp, tmp_path):
    made = []

    def make(responder=fast_responder, cfg=None):
        ctxs = []

        def factory(c):
            ctx = Ctx(responder)
            ctxs.append(ctx)
            return ctx

        settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        w = MainWindow(cfg or Config(), engine_factory=factory, settings=settings, show_dialogs=False)
        w.ctxs = ctxs
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()


# ------------------------------------------------------------ 表示用HTML

def test_html_escapes_untrusted_pdf_text_and_shows_cards_and_sources():
    doc = _doc()
    ex = extract_task_cards(doc)
    evil = '<img src="http://x/y.png"><script>alert(1)</script>'
    res = SummaryResult(mode="detail", doc_kind="general", source_name="x.pdf", page_count=8,
                        sections=[Section("主要ポイント", [Claim(evil, block_ids=[3])])],
                        evidence={b.id: b for b in doc}, tasks=ex.cards, task_outline=ex.outline,
                        warnings=["<b>注意</b>"])
    html = result_to_html(res, Colors()).replace("\u200b", "")          # 改行位置（ゼロ幅スペース）は比較から除く
    assert "<img" not in html and "<script" not in html and "&lt;img" in html     # タグとして解釈させない
    assert "<b>注意</b>" not in html
    assert "課題1" in html and "設定表の作成" in html and "<pre" in html
    assert "[root@L198]# /sbin/service httpd restart" in html.replace("&#x27;", "'")
    assert "参照した原文" not in html
    assert "参照した原文" in result_to_html(res, Colors(), show_sources=True)


def test_html_marks_unresolved_claims_and_empty_sections():
    res = SummaryResult(mode="fast", doc_kind="general", source_name="x", page_count=1,
                        sections=[Section("主要ポイント", [Claim("根拠なし", status="unresolved")]), Section("やること・手順", [])])
    html = result_to_html(res, Colors(warn="#ff0000"))
    assert "参照箇所未特定" in html and "#ff0000" in html and "該当なし" in html


# ------------------------------------------------------------ 見積り・進み具合の表示

def test_format_helpers():
    assert format_estimate(40) == "約1分"
    assert format_estimate(215) == "約3〜5分"
    assert format_elapsed(83) == "1:23" and format_elapsed(5) == "0:05"


def test_stage_fraction_is_monotone_and_fast_has_none():
    seq = [stage_fraction("map", d, 8) for d in range(9)] + [stage_fraction("reduce", 1, 1), stage_fraction("final", 0, 1),
                                                          stage_fraction("final", 1, 1)]
    assert seq == sorted(seq) and seq[0] == 0.0 and seq[-1] == 1.0
    assert stage_fraction("fast", 0, 1) is None


def test_estimate_scales_with_length_mode_and_measured_speed(tmp_path):
    cfg = Config()
    short = prepare(pdf(tmp_path, 2), cfg)
    long = prepare(pdf(tmp_path, 40), cfg)
    assert estimate_seconds(long, cfg, "detail") > estimate_seconds(short, cfg, "detail")
    assert estimate_seconds(long, cfg, "fast") == pytest.approx(estimate_seconds(long, cfg, "fast"))
    assert estimate_seconds(long, cfg, "fast") < estimate_seconds(long, cfg, "detail")
    assert estimate_seconds(long, cfg, "detail", prompt_tps=120, gen_tps=30) < estimate_seconds(long, cfg, "detail")
    assert estimate_seconds(long, cfg.with_overrides(time_budget_s=60), "detail") <= 60 + 40 + 1e-6      # 時間予算が上限になる


# ------------------------------------------------------------ ウィンドウの流れ

def test_load_summarize_copy_and_save(qapp, window, tmp_path):
    w = window()
    w.show()
    assert not w.btn_run.isEnabled()                                   # PDFを読むまでは押せない
    w.load_pdf(pdf(tmp_path))
    wait_until(qapp, lambda: w.prepared is not None)
    assert "2ページ" in w.file_label.text() and "資料.pdf" in w.file_label.text()
    assert "目安" in w.fast_hint.text() and w.btn_run.isEnabled()

    w.start()
    wait_until(qapp, lambda: w.result is not None)
    assert w.ctxs and w.ctxs[0].stopped                                # サーバー（エンジン）は必ず停止している
    assert "できました" in w.status.text() and not w._running
    assert "正規化は冗長性を減らし整合性を保つ" in w.browser.toPlainText()
    assert w.btn_copy.isEnabled() and w.btn_save.isEnabled() and w.btn_run.text() == "要約する"
    assert "入力処理速度" in w.metrics_label.text()

    w.copy_result()
    assert QApplication.clipboard().text() == w.result_text() and "## 概要" in w.result_text()
    out = tmp_path / "out.md"
    w.save_to(str(out))
    assert out.read_text(encoding="utf-8") == w.result_text()
    assert w.default_save_name() == "資料_要約.pdf"

    pdf_out = tmp_path / "out.pdf"                                     # 拡張子 .pdf なら、PDFとして書き出す
    w.save_to(str(pdf_out))
    import fitz
    with fitz.open(str(pdf_out)) as d:
        text = "".join(pg.get_text() for pg in d)
        assert d.page_count >= 1 and d.metadata.get("title") == "LocalDoc AI 要約"
    assert "資料.pdf" in text and "概要" in text and "正規化は冗長性を減らし整合性を保つ" in text
    assert "🌼" not in text                                            # PDFには絵文字を使わない
    assert not w.error.isVisible()
    bad = tmp_path / "no_such_dir" / "x.pdf"                           # 書けない場所はエラー表示（落ちない）
    w.save_to(str(bad))
    assert "保存できませんでした" in w.error.text()

    w.chk_sources.setChecked(True)
    assert "参照した原文" in w.browser.toPlainText() and "参照した原文" in w.result_text()


def test_controls_are_locked_while_running_and_cancel_stops_the_engine(qapp, window, tmp_path):
    gate, started = threading.Event(), threading.Event()

    def responder(system, user, n):
        started.set()
        gate.wait(10)
        return "- 正規化は冗長性を減らす [B%d]" % ids_in(user)[0] if is_map(user) else "概要である。"

    cfg = Config(ctx_size=8192, chunk_tokens=300, single_pass_tokens=10, reduce_input_tokens=3500).validate()
    w = window(responder, cfg)
    w.show()
    w.load_pdf(pdf(tmp_path, 6))
    wait_until(qapp, lambda: w.prepared is not None)
    w.rb_detail.setChecked(True)
    w.start()
    wait_until(qapp, started.is_set)
    assert w._running and w.btn_run.text() == "中止" and w.btn_run.isEnabled()
    assert not w.btn_open.isEnabled() and not w.rb_fast.isEnabled() and not w.btn_copy.isEnabled()

    w.cancel()
    assert w.status.text() == "中止しています…" and not w.btn_run.isEnabled()
    gate.set()
    wait_until(qapp, lambda: not w._running)
    wait_until(qapp, lambda: w.status.text() == "中止しました")
    assert w.result is None and w.ctxs[0].stopped
    assert w.btn_run.text() == "要約する" and w.btn_run.isEnabled() and w.btn_open.isEnabled()


def test_missing_model_is_shown_as_a_clear_error_without_crashing(qapp, tmp_path):
    def factory(cfg):
        raise AssetNotFoundError("モデルファイルが見つかりません。tools\\setup_assets.py で取得してください。")

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    w = MainWindow(Config(), engine_factory=factory, settings=settings, show_dialogs=False)
    try:
        w.load_pdf(pdf(tmp_path))
        wait_until(qapp, lambda: w.prepared is not None)
        w.start()
        wait_until(qapp, lambda: not w._running)
        wait_until(qapp, lambda: not w.error.isHidden())
        assert "モデルファイルが見つかりません" in w.error.text() and w.status.text() == "要約できませんでした"
        assert w.btn_run.isEnabled() and w.result is None
    finally:
        w.close()


def test_loading_a_broken_pdf_shows_an_error_and_keeps_the_window_usable(qapp, window, tmp_path):
    bad = tmp_path / "壊れている.pdf"
    bad.write_bytes(b"not a pdf")
    w = window()
    w.load_pdf(str(bad))
    wait_until(qapp, lambda: not w._loading)
    assert w.prepared is None and not w.error.isHidden() and w.error.text()
    assert not w.btn_run.isEnabled() and w.btn_open.isEnabled()
    w.load_pdf(pdf(tmp_path))                                           # 次のPDFは普通に読める
    wait_until(qapp, lambda: w.prepared is not None)
    assert w.error.isHidden()


def test_only_a_single_local_pdf_can_be_dropped(qapp, tmp_path):
    class Ev:
        def __init__(self, md):
            self._md = md

        def mimeData(self):
            return self._md

    def md(*paths):
        m = QMimeData()
        m.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return m

    ok = str(tmp_path / "a.PDF")
    assert MainWindow._pdf_from_mime(Ev(md(ok))) == ok
    assert MainWindow._pdf_from_mime(Ev(md(str(tmp_path / "a.txt")))) == ""
    assert MainWindow._pdf_from_mime(Ev(md(ok, ok))) == ""
    web = QMimeData()
    web.setUrls([QUrl("https://example.com/a.pdf")])
    assert MainWindow._pdf_from_mime(Ev(web)) == ""
    assert MainWindow._pdf_from_mime(Ev(QMimeData())) == ""


def test_closing_during_a_run_cancels_and_waits_for_the_engine_to_stop(qapp, window, tmp_path):
    gate, started = threading.Event(), threading.Event()

    def responder(system, user, n):
        started.set()
        gate.wait(10)
        return fast_responder(system, user, n)

    cfg = Config(ctx_size=8192, chunk_tokens=300, single_pass_tokens=10).validate()
    w = window(responder, cfg)
    w.load_pdf(pdf(tmp_path, 6))
    wait_until(qapp, lambda: w.prepared is not None)
    w.rb_detail.setChecked(True)
    w.start()
    wait_until(qapp, started.is_set)
    threading.Timer(0.3, gate.set).start()
    w.close()                                                           # 中止して、エンジン停止まで待ってから閉じる
    assert w.ctxs[0].stopped and not w._thread.isRunning()


def test_mode_is_remembered_between_launches(qapp, tmp_path):
    ini = str(tmp_path / "s.ini")
    w1 = MainWindow(Config(), settings=QSettings(ini, QSettings.Format.IniFormat), show_dialogs=False)
    w1.rb_detail.setChecked(True)
    w1.close()
    w2 = MainWindow(Config(), settings=QSettings(ini, QSettings.Format.IniFormat), show_dialogs=False)
    assert w2.mode == "detail"
    w2.close()


# ---------------------------------------------------------------- 見た目（テーマ）

def test_bundled_fonts_and_their_licenses_are_present():
    from app.gui.theme import BODY_FILES, FONT_DIR, POP_FILES
    for name in (*BODY_FILES, *POP_FILES, "OFL-HachiMaruPop.txt", "OFL-ZenMaruGothic.txt"):
        assert (FONT_DIR / name).stat().st_size > 1000, name
    assert "SIL OPEN FONT LICENSE" in (FONT_DIR / "OFL-ZenMaruGothic.txt").read_text(encoding="utf-8").upper()


def test_bundled_fonts_load_and_stylesheet_is_well_formed(qapp):
    from app.gui.theme import build_stylesheet, load_fonts, theme_colors
    fonts = load_fonts()
    assert fonts.loaded and fonts is load_fonts()          # 同梱フォントを読み込めて、2回目は同じ結果
    qss = build_stylesheet(theme_colors())
    assert "{{" not in qss and "}}" not in qss
    assert qss.count("{") == qss.count("}")
    assert fonts.pop in qss and fonts.body in qss and "#FFF3F7" in qss


def test_html_uses_chips_and_pink_palette():
    res = SummaryResult(mode="fast", doc_kind="general", source_name="x", page_count=1,
                        sections=[Section("概要", [Claim("要約文", status="overall")])])
    html = result_to_html(res, Colors()).replace("\u200b", "")          # 改行位置（ゼロ幅スペース）は比較から除く
    assert "#C2457F" in html and "Hachi Maru Pop" in html and "🌼" in html


def test_window_is_styled_and_keeps_function_text(qapp, window):
    w = window()
    assert w.colors.accent == "#F06C9B"
    assert "LocalDoc AI" in w.windowTitle()
    assert "#F06C9B" in w.styleSheet()
    assert w.btn_run.text() == "要約する" and not w.windowIcon().isNull()


def test_long_unbreakable_strings_get_break_points_only_in_the_display():
    from app.gui.html import wrap_esc
    url = "http://example/public/test.html?a=1"
    w = wrap_esc(url)
    assert w != url and w.replace("​", "") == url
    assert wrap_esc("日本語 text") == "日本語 text"                      # 普通の文は変えない
    assert "<" not in wrap_esc("<b>/x</b>").replace("&lt;", "").replace("&gt;", "")   # エスケープは維持


def test_header_title_and_privacy_note_are_not_clipped_at_the_smallest_window(qapp, window):
    """配布版の実機で、タイトルが「LocalDoc F」のように切れた。最小幅でも、どちらの文字も切れない。"""
    from PySide6.QtWidgets import QLabel
    w = window()
    w.resize(w.minimumSize())
    w.show()
    qapp.processEvents()
    for name in ("title", "pill"):
        lab = next(x for x in w.header.findChildren(QLabel) if x.objectName() == name)
        assert lab.width() >= lab.sizeHint().width(), (name, lab.width(), lab.sizeHint().width())
