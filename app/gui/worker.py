"""別スレッドで動かす処理（画面を固めないため）。

- LoadWorker   : PDFの読み込みと整形（LLM不要・短時間）
- SummaryWorker: モデルの起動 → 要約 → 終了。中止は CancelToken で行い、llama-server は必ず停止する

Qt のシグナルで結果を画面に返す。例外は利用者向けの日本語メッセージにして failed で返す
（文書の内容を含みうる例外の詳細は、画面には出さない）。
"""
from __future__ import annotations

import sys
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from app.core.cancel import CancelToken
from app.core.config import Config, resolve_model, resolve_server
from app.core.errors import Cancelled, LocalDocError
from app.core.metrics import RunMetrics
from app.pipeline import Prepared, prepare, summarize

EngineFactory = Callable[[Config], object]          # with 文で使える推論エンジンを返す

# 要約の段階 → 利用者向けの表示
STAGE_LABELS = {
    "fast": "重要な部分を読んで要約しています",
    "map": "各部分を読んでいます",
    "reduce": "要点をまとめています",
    "final": "仕上げています",
    "overview": "概要を作っています",
}


def stage_fraction(stage: str, done: int, total: int) -> float | None:
    """詳細モードの進み具合（0〜1）。段階ごとの重みは実測の時間配分の概算。fast は None（時間で推定する）。"""
    frac = done / total if total else 0.0
    if stage == "map":
        return round(0.90 * frac, 4)
    if stage == "reduce":
        return round(0.90 + 0.05 * frac, 4)
    if stage in ("final", "overview"):
        return round(0.95 + 0.05 * frac, 4)
    return None


def default_engine_factory(cfg: Config):
    from app.llm.engine import LlamaServerEngine
    return LlamaServerEngine(cfg, resolve_model(cfg), resolve_server(cfg))


class LoadWorker(QObject):
    loaded = Signal(object)          # Prepared
    failed = Signal(str)

    def __init__(self, pdf_path: str, cfg: Config) -> None:
        super().__init__()
        self._path, self._cfg = pdf_path, cfg

    @Slot()
    def run(self) -> None:
        try:
            self.loaded.emit(prepare(self._path, self._cfg, RunMetrics()))
        except LocalDocError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr) if sys.stderr else None
            self.failed.emit(f"PDFを読み込めませんでした（{type(e).__name__}）。")


class SummaryWorker(QObject):
    stage = Signal(str)                          # 状況の文言
    progress = Signal(str, int, int)             # 段階, 完了, 全体
    finished = Signal(object, object, float)     # SummaryResult, RunMetrics, 経過秒
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, prepared: Prepared, cfg: Config, mode: str, *,
                 engine_factory: EngineFactory | None = None, kind: str = "auto") -> None:
        super().__init__()
        self._prep, self._cfg, self._mode, self._kind = prepared, cfg, mode, kind
        self._factory = engine_factory or default_engine_factory
        self._cancel = CancelToken()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        t0 = time.perf_counter()
        metrics = RunMetrics()
        try:
            self.stage.emit("モデルを起動しています…")
            with self._factory(self._cfg) as engine:
                metrics.model_start_s = getattr(engine, "startup_s", 0.0) or 0.0
                self._cancel.raise_if_set()
                self.stage.emit("要約しています…")
                result = summarize(
                    self._prep, self._cfg, engine, mode=self._mode, kind=self._kind, metrics=metrics,
                    progress=lambda st, done, total: self.progress.emit(st, done, total),
                    cancel=self._cancel,
                )
                metrics.peak_rss_mb = getattr(engine, "peak_rss_mb", None)
            self.finished.emit(result, metrics, time.perf_counter() - t0)
        except Cancelled:
            self.cancelled.emit()
        except LocalDocError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            if sys.stderr:
                traceback.print_exc(file=sys.stderr)
            self.failed.emit(f"予期しないエラーが起きました（{type(e).__name__}）。もう一度試してください。")
