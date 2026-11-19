"""アプリ共通の例外。

メッセージは利用者にそのまま見せられる日本語にする（F-06 / 受け入れ条件）。
"""
from __future__ import annotations


class LocalDocError(Exception):
    """利用者に表示してよいメッセージを持つ基底例外。"""


class PdfError(LocalDocError):
    """PDFを開けない・読めない・上限を超えている。"""


class EngineError(LocalDocError):
    """推論サーバーの起動・通信に関するエラー。"""


class ServerStartError(EngineError):
    """llama-server の起動に失敗した。"""


class ContextOverflowError(EngineError):
    """入力がモデルのコンテキスト長を超えた（呼び出し側で入力を減らして再試行する）。"""


class SummaryError(LocalDocError):
    """要約処理そのものの失敗（入力が空、出力が解釈できない等）。"""


class AssetNotFoundError(LocalDocError):
    """モデルファイルまたは llama-server が見つからない。"""


class Cancelled(LocalDocError):
    """利用者による中止。"""
