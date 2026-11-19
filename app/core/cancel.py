"""キャンセル用トークン。

GUI（Phase 3）のワーカースレッドからも、CLIの Ctrl+C からも同じ仕組みで中止できるようにする。
推論エンジンは set() された時に通信を切断するコールバックを登録する。
"""
from __future__ import annotations

import threading
from typing import Callable

from .errors import Cancelled


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def set(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb()
            except Exception:  # 中止処理の失敗で別の例外を出さない
                pass

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_set(self) -> None:
        if self._event.is_set():
            raise Cancelled("処理が中止されました。")

    def add_callback(self, fn: Callable[[], None]) -> Callable[[], None]:
        """中止時に呼ぶ関数を登録し、登録解除用の関数を返す。既に中止済みなら即座に呼ぶ。"""
        with self._lock:
            already = self._event.is_set()
            if not already:
                self._callbacks.append(fn)
        if already:
            try:
                fn()
            except Exception:
                pass

        def remove() -> None:
            with self._lock:
                if fn in self._callbacks:
                    self._callbacks.remove(fn)

        return remove
