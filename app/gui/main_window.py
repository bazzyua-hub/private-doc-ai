"""メインウィンドウ。

流れ: PDFを選ぶ（またはドロップ）→ 読み込み → モード選択 → 「要約する」→ 進捗 → 結果表示 → コピー/保存。
処理はすべて別スレッド（worker.py）。資料の内容は、利用者が「コピー」「保存」を押したとき以外、どこにも書き出さない。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from app.core.config import Config
from app.core.metrics import RunMetrics
from app.pipeline import Prepared, estimate_seconds
from app.summary.schemas import SummaryResult

from .export import pdf_html, write_pdf
from .html import Colors, esc, result_to_html
from .theme import build_stylesheet, light_palette, make_icon, theme_colors
from .worker import STAGE_LABELS, EngineFactory, LoadWorker, SummaryWorker, stage_fraction

APP_TITLE = "LocalDoc AI"
TAGLINE = "PDFをぎゅっと、かんたん要約"
PRIVACY_NOTE = "🔒 この資料はこのPCの中だけで処理されるよ（外部には送信されません）"
DROP_HINT = "📄 ここにPDFをドラッグ＆ドロップしてね（右のボタンから選んでもOK）"
PLACEHOLDER = "PDFを選んで「要約する」を押すと、ここに結果が出るよ 🌸"
_BAR_MAX = 1000


def format_estimate(sec: float) -> str:
    lo = max(1, round(sec * 0.8 / 60))
    hi = max(lo, round(sec * 1.3 / 60))
    return f"約{lo}分" if lo == hi else f"約{lo}〜{hi}分"


def format_elapsed(sec: float) -> str:
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None, *, engine_factory: EngineFactory | None = None,
                 settings: QSettings | None = None, show_dialogs: bool = True) -> None:
        super().__init__()
        self.cfg = (cfg or Config()).validate()
        self._engine_factory = engine_factory
        self._settings = settings or QSettings("LocalDocAI", "LocalDocAI")
        self.show_dialogs = show_dialogs          # テストでは False（ダイアログで止まらないように）

        self.prepared: Prepared | None = None
        self.pdf_path = ""
        self.result: SummaryResult | None = None
        self.metrics: RunMetrics | None = None
        self._running = False
        self._loading = False
        self._run_mode = "fast"
        self._est_sec = 0.0
        self._elapsed = 0.0
        self._thread: QThread | None = None
        self._worker: SummaryWorker | None = None
        self._load_thread: QThread | None = None
        self._load_worker: LoadWorker | None = None

        self.colors: Colors = theme_colors()          # 配色（同梱フォントの読み込みもここで行う）
        self.setPalette(light_palette(self.colors))
        self.setStyleSheet(build_stylesheet(self.colors))
        self.setWindowIcon(make_icon(self.colors))
        self.setWindowTitle(APP_TITLE)
        self.setAcceptDrops(True)
        self.resize(940, 780)
        self.setMinimumSize(720, 600)
        self._build_ui()
        self._restore_settings()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._refresh_controls()

    # ------------------------------------------------------------ 画面の組み立て

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 18, 22, 12)
        lay.setSpacing(12)

        self.header = QFrame()
        self.header.setObjectName("header")
        hl = QVBoxLayout(self.header)                  # タイトルと「この資料はこのPCの中だけ」の帯は縦に並べる（横並びだと狭い幅でタイトルが切れる）
        hl.setContentsMargins(22, 14, 22, 14)
        hl.setSpacing(8)
        title = QLabel("🌸 " + APP_TITLE)
        title.setObjectName("title")
        tagline = QLabel(TAGLINE)
        tagline.setObjectName("tagline")
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(title)
        titles.addWidget(tagline)
        privacy = QLabel(PRIVACY_NOTE)
        privacy.setObjectName("pill")
        hl.addLayout(titles)
        hl.addWidget(privacy, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.header)

        # ファイル選択（ドロップ領域）
        self.drop = QFrame()
        self.drop.setObjectName("drop")
        dl = QHBoxLayout(self.drop)
        dl.setContentsMargins(20, 16, 20, 16)
        self.file_label = QLabel(DROP_HINT)
        self.file_label.setWordWrap(True)
        self.btn_open = QPushButton("📂 ファイルを選ぶ")
        self.btn_open.clicked.connect(self.choose_file)
        dl.addWidget(self.file_label, 1)
        dl.addWidget(self.btn_open)
        lay.addWidget(self.drop)
        self.notice = QLabel()
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)

        # モード・実行
        self.rb_fast = QRadioButton("⚡ 高速モード")
        self.rb_detail = QRadioButton("🔍 詳細モード")
        self.rb_fast.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_fast)
        self.mode_group.addButton(self.rb_detail)
        self.mode_group.buttonToggled.connect(lambda *_: self._refresh_controls())
        self.fast_hint = QLabel("重要な部分だけを読みます")
        self.detail_hint = QLabel("全体を読みます")
        for h in (self.fast_hint, self.detail_hint):
            h.setObjectName("muted")
        modes = QVBoxLayout()
        modes.setSpacing(2)
        for rb, hint in ((self.rb_fast, self.fast_hint), (self.rb_detail, self.detail_hint)):
            row = QHBoxLayout()
            row.addWidget(rb)
            row.addWidget(hint)
            row.addStretch(1)
            modes.addLayout(row)
        self.btn_run = QPushButton("要約する")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(50)
        self.btn_run.setMinimumWidth(170)
        self.btn_run.clicked.connect(self._on_run_clicked)
        run_row = QHBoxLayout()
        run_row.addLayout(modes, 1)
        run_row.addWidget(self.btn_run, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(run_row)

        # 進捗
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.elapsed = QLabel("")
        self.elapsed.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, _BAR_MAX)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        srow.addWidget(self.elapsed)
        lay.addLayout(srow)
        lay.addWidget(self.bar)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName("error")
        self.error.hide()
        lay.addWidget(self.error)

        # 結果
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        bf = self.browser.font()
        bf.setPointSize(bf.pointSize() + 1)
        self.browser.setFont(bf)
        lay.addWidget(self.browser, 1)

        # 下段
        self.chk_sources = QCheckBox("根拠の原文も見せる")
        self.chk_sources.toggled.connect(lambda *_: self._render())
        self.btn_copy = QPushButton("📋 コピー")
        self.btn_copy.clicked.connect(self.copy_result)
        self.btn_save = QPushButton("💾 保存")
        self.btn_save.clicked.connect(self.save_result)
        self.btn_metrics = QToolButton()
        self.btn_metrics.setText("計測を見る")
        self.btn_metrics.setCheckable(True)
        self.btn_metrics.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_metrics.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_metrics.toggled.connect(self._toggle_metrics)
        bottom = QHBoxLayout()
        bottom.addWidget(self.chk_sources)
        bottom.addWidget(self.btn_metrics)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_copy)
        bottom.addWidget(self.btn_save)
        lay.addLayout(bottom)
        self.metrics_label = QLabel()
        self.metrics_label.setObjectName("muted")
        self.metrics_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metrics_label.hide()
        mono = self.metrics_label.font()
        mono.setFamilies(["Consolas", "Menlo", "Noto Sans Mono CJK JP", "monospace"])
        self.metrics_label.setFont(mono)
        lay.addWidget(self.metrics_label)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.choose_file)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        self.browser.setHtml(f'<p style="color:{self.colors.muted}; font-size:12pt;">{esc(PLACEHOLDER)}</p>')

    # ------------------------------------------------------------ 状態の反映

    @property
    def mode(self) -> str:
        return "fast" if self.rb_fast.isChecked() else "detail"

    def _refresh_controls(self) -> None:
        busy = self._running or self._loading
        self.btn_open.setEnabled(not busy)
        self.rb_fast.setEnabled(not busy)
        self.rb_detail.setEnabled(not busy)
        self.btn_run.setEnabled(self._running or (self.prepared is not None and not self._loading))
        self.btn_run.setText("中止" if self._running else "要約する")
        has = self.result is not None and not self._running
        self.btn_copy.setEnabled(has)
        self.btn_save.setEnabled(has)
        self.chk_sources.setEnabled(has)
        if self.prepared is not None:
            for mode, hint, base in (("fast", self.fast_hint, "重要な部分だけを読みます"),
                                     ("detail", self.detail_hint, "全体を読みます")):
                hint.setText(f"{base}（目安 {format_estimate(estimate_seconds(self.prepared, self.cfg, mode))}）")

    def _set_error(self, msg: str) -> None:
        self.error.setText(msg)
        self.error.setVisible(bool(msg))

    def _toggle_metrics(self, on: bool) -> None:
        self.btn_metrics.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.metrics_label.setVisible(on and bool(self.metrics_label.text()))

    # ------------------------------------------------------------ PDFの読み込み

    def choose_file(self) -> None:
        if self._running or self._loading:
            return
        start = self._settings.value("last_dir", "", type=str) or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "要約するPDFを選ぶ", start, "PDFファイル (*.pdf)")
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str) -> None:
        if self._running or self._loading:
            return
        self._settings.setValue("last_dir", str(Path(path).parent))
        self.pdf_path = path
        self.prepared = None
        self.result = None
        self.metrics = None
        self._loading = True
        self._set_error("")
        self.notice.hide()
        self._show_placeholder()
        self.metrics_label.clear()
        self.metrics_label.hide()
        self.file_label.setText(f"📄 {esc(Path(path).name)} を読み込んでいるよ…")
        self.status.setText("PDFを読み込んでいるよ…")
        self.bar.setRange(0, 0)                             # 動き続ける表示
        self._refresh_controls()

        self._load_thread = QThread(self)
        self._load_worker = LoadWorker(path, self.cfg)
        self._load_worker.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._load_worker.run)
        self._load_worker.loaded.connect(self._on_loaded)
        self._load_worker.failed.connect(self._on_load_failed)
        for sig in (self._load_worker.loaded, self._load_worker.failed):
            # 直接接続: メインスレッドが終了待ちで止まっていても、ワーカー側から終了を指示できるようにする
            sig.connect(self._load_thread.quit, Qt.ConnectionType.DirectConnection)
        self._load_thread.start()

    def _end_loading(self) -> None:
        self._loading = False
        self.bar.setRange(0, _BAR_MAX)
        self.bar.setValue(0)

    def _on_loaded(self, prep: Prepared) -> None:
        self._end_loading()
        self.prepared = prep
        name = Path(self.pdf_path).name
        ext = prep.extraction
        kind = {"paper": "論文", "general": "一般資料"}.get(prep.cleaned.doc_kind, prep.cleaned.doc_kind)
        self.file_label.setText(f"📄 <b>{esc(name)}</b>　{ext.page_count}ページ・{ext.total_chars:,}文字・{esc(kind)}と判定")
        if prep.warnings:
            self.notice.setText(" ／ ".join(prep.warnings))
            self.notice.show()
        self.status.setText("準備OK！モードを選んで「要約する」を押してね")
        self._refresh_controls()

    def _on_load_failed(self, msg: str) -> None:
        self._end_loading()
        self.file_label.setText(DROP_HINT)
        self.status.setText("")
        self._set_error(msg)
        self._refresh_controls()
        if self.show_dialogs:
            QMessageBox.warning(self, APP_TITLE, msg)

    # ------------------------------------------------------------ 要約の実行

    def _on_run_clicked(self) -> None:
        if self._running:
            self.cancel()
        else:
            self.start()

    def start(self) -> None:
        if self.prepared is None or self._running or self._loading:
            return
        self._run_mode = self.mode
        self._est_sec = estimate_seconds(self.prepared, self.cfg, self._run_mode) + 3.0     # + モデル起動
        self._running = True
        self._elapsed = 0.0
        self.result = None
        self.metrics = None
        self._set_error("")
        self._show_placeholder()
        self.metrics_label.clear()
        self.metrics_label.hide()
        self.bar.setRange(0, _BAR_MAX)
        self.bar.setValue(0)
        self.status.setText("モデルを起動しています…")
        self.elapsed.setText("経過 0:00")
        self._refresh_controls()

        self._thread = QThread(self)
        self._worker = SummaryWorker(self.prepared, self.cfg, self._run_mode, engine_factory=self._engine_factory)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.stage.connect(self.status.setText)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        for sig in (self._worker.finished, self._worker.failed, self._worker.cancelled):
            sig.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._timer.start()
        self._clock_start = _now()
        self._thread.start()

    def cancel(self) -> None:
        if self._running and self._worker is not None:
            self._worker.cancel()
            self.status.setText("中止しています…")
            self.btn_run.setEnabled(False)

    def _tick(self) -> None:
        self._elapsed = _now() - self._clock_start
        self.elapsed.setText(f"経過 {format_elapsed(self._elapsed)}")
        if self._run_mode == "fast" and self._est_sec > 0:             # 高速は1回の呼び出しなので、時間から進み具合を推定する
            self.bar.setValue(int(_BAR_MAX * min(0.95, self._elapsed / self._est_sec)))

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        label = STAGE_LABELS.get(stage, "要約しています")
        self.status.setText(f"{label}…" + (f"（{done}/{total}）" if stage in ("map", "reduce") and total else ""))
        frac = stage_fraction(stage, done, total)
        if frac is not None and self._run_mode == "detail":
            self.bar.setValue(int(_BAR_MAX * frac))

    def _end_run(self) -> None:
        self._running = False
        self._timer.stop()

    def _on_finished(self, result: SummaryResult, metrics: RunMetrics, elapsed: float) -> None:
        self._end_run()
        self.result, self.metrics = result, metrics
        self.bar.setValue(_BAR_MAX)
        self.status.setText(f"✨ できました（要約 {metrics.summarize_s:.0f}秒）")
        self.elapsed.setText(f"全体 {format_elapsed(elapsed)}")
        self.metrics_label.setText("\n".join(metrics.report_lines()))
        self._toggle_metrics(self.btn_metrics.isChecked())
        self._render()
        self._refresh_controls()

    def _on_failed(self, msg: str) -> None:
        self._end_run()
        self.bar.setValue(0)
        self.status.setText("要約できませんでした")
        self._set_error(msg)
        self._refresh_controls()
        if self.show_dialogs:
            QMessageBox.warning(self, APP_TITLE, msg)

    def _on_cancelled(self) -> None:
        self._end_run()
        self.bar.setValue(0)
        self.status.setText("中止しました")
        self._refresh_controls()

    def _render(self) -> None:
        if self.result is None:
            return
        bar = self.browser.verticalScrollBar().value()
        self.browser.setHtml(result_to_html(self.result, self.colors,
                                            show_sources=self.chk_sources.isChecked()))
        self.browser.verticalScrollBar().setValue(bar)

    # ------------------------------------------------------------ コピー・保存（利用者が押したときだけ書き出す）

    def result_text(self) -> str:
        return self.result.render_text(show_sources=self.chk_sources.isChecked()) if self.result else ""

    def copy_result(self) -> None:
        if self.result is None:
            return
        QGuiApplication.clipboard().setText(self.result_text())
        self.statusBar().showMessage("クリップボードにコピーしました", 4000)

    def default_save_name(self) -> str:
        return (Path(self.pdf_path).stem or "要約") + "_要約.pdf"

    def save_result(self) -> None:
        if self.result is None:
            return
        start = str(Path(self._settings.value("last_dir", "", type=str) or Path.home()) / self.default_save_name())
        path, _ = QFileDialog.getSaveFileName(self, "要約を保存", start,
                                              "PDF (*.pdf);;Markdown (*.md);;テキスト (*.txt)")
        if path:
            self.save_to(path)

    def save_to(self, path: str) -> None:
        try:
            if path.lower().endswith(".pdf") and self.result is not None:
                write_pdf(pdf_html(self.result, self.colors, show_sources=self.chk_sources.isChecked()), path, self.colors)
                if not Path(path).is_file():
                    raise OSError("PDFを書き出せませんでした")
            else:
                Path(path).write_text(self.result_text(), encoding="utf-8")
        except OSError as e:
            self._set_error(f"保存できませんでした: {e.strerror or e}")
            return
        self.statusBar().showMessage(f"保存しました: {path}", 6000)

    # ------------------------------------------------------------ ドラッグ＆ドロップ

    @staticmethod
    def _pdf_from_mime(event) -> str:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(".pdf"):
            return urls[0].toLocalFile()
        return ""

    def _set_dragging(self, on: bool) -> None:
        self.drop.setProperty("dragging", on)
        self.drop.style().unpolish(self.drop)
        self.drop.style().polish(self.drop)

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if not (self._running or self._loading) and self._pdf_from_mime(e):
            e.acceptProposedAction()
            self._set_dragging(True)
        else:
            e.ignore()

    def dragLeaveEvent(self, e) -> None:
        self._set_dragging(False)

    def dropEvent(self, e: QDropEvent) -> None:
        self._set_dragging(False)
        path = self._pdf_from_mime(e)
        if path:
            e.acceptProposedAction()
            self.load_pdf(path)

    # ------------------------------------------------------------ 設定・終了

    def _restore_settings(self) -> None:
        if self._settings.value("mode", "fast", type=str) == "detail":
            self.rb_detail.setChecked(True)
        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, e: QCloseEvent) -> None:
        if self._running:
            if self.show_dialogs and QMessageBox.question(
                    self, APP_TITLE, "要約の途中です。中止して終了しますか？") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.cancel()
            self._wait_thread(self._thread, 20.0)             # llama-server の停止まで待つ
        self._wait_thread(self._load_thread, 5.0)
        self._settings.setValue("mode", self.mode)
        self._settings.setValue("geometry", self.saveGeometry())
        e.accept()


    @staticmethod
    def _wait_thread(thread: QThread | None, timeout_s: float) -> None:
        """スレッドの終了を待つ。待つ間も画面のイベントを処理する（シグナルの受け渡しが止まらないように）。"""
        if thread is None:
            return
        deadline = _now() + timeout_s
        while thread.isRunning() and _now() < deadline:
            QGuiApplication.processEvents()
            thread.wait(50)


def _now() -> float:
    import time
    return time.perf_counter()
