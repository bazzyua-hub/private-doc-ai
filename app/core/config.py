"""設定と、モデル／llama-server の場所の解決。

数値の既定値は「出発点」であり、性能を保証するものではない。
対象PCで tools/bench.py を使って実測し、調整すること。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import AssetNotFoundError

APP_VERSION = "1.0.0"
LOOPBACK = "127.0.0.1"


def is_frozen() -> bool:
    """PyInstaller で固めた配布版として動いているか。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """models/ と llama/ を置く基準のフォルダ。

    開発時はプロジェクトのフォルダ。配布版では LocalDocAI.exe があるフォルダ
    （__file__ は展開先の内部フォルダ _internal を指すため使えない）。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


APP_ROOT = app_root()


def asset_hint(kind: str) -> str:
    """モデル／llama-server が無いときの案内。開発版と配布版で、取得の方法が違う。"""
    if is_frozen():
        if kind == "llama":
            return "配布フォルダの llama フォルダに llama-server.exe を置いてください（配布zipには同梱されています。zipを展開し直してください）。"
        return ("配布フォルダの models フォルダにモデル（.gguf）を置いてください。"
                "同梱の get_model.bat を実行すると取得できます（ネット接続を使うのはこの操作だけです）。")
    if kind == "llama":
        return "`python tools/setup_assets.py llama` で取得してください。"
    return "`python tools/setup_assets.py model 1.7b` で取得してください。"

# 動作を確認した llama.cpp のリリース（Windows CPU 版 zip が存在することを確認済み）。
LLAMA_CPP_TAG = "b12503"

SERVER_EXE = "llama-server.exe" if sys.platform == "win32" else "llama-server"


def default_threads() -> int:
    """論理コア数の半分（≒物理コア数）を出発点にする。最適値は機種依存なので要ベンチ。"""
    return max(1, (os.cpu_count() or 4) // 2)


@dataclass
class Config:
    # --- 場所 ---
    models_dir: Path = APP_ROOT / "models"
    llama_dir: Path = APP_ROOT / "llama"

    # --- 推論サーバー（S-04: 127.0.0.1 固定） ---
    host: str = LOOPBACK
    threads: int = default_threads()
    threads_batch: int | None = None     # 入力処理用スレッド数。None なら threads と同じ
    ctx_size: int = 8192
    startup_timeout_s: float = 180.0
    request_timeout_s: float = 900.0     # 1回の通信で待てる最大時間（長い入力処理を含む）

    # --- サンプリング（非思考モード。モデルカードの推奨値を参考に、要約向けに低めの温度） ---
    temperature: float = 0.4
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 0.0
    cache_prompt: bool = True            # 直前と同じ先頭部分の入力処理を再利用する（ベンチでは無効にして公平に測る）

    # --- 読み込み上限（S-03） ---
    max_pdf_mb: float = 200.0
    max_pages: int = 500
    max_chars_per_page: int = 50_000

    # --- 高速モード ---
    fast_input_tokens: int = 1800        # 読む量を少し増やす（重要な情報の取りこぼしを減らす）
    fast_max_output: int = 650           # 概要（200〜350字）＋主要ポイント5〜8項目が収まる長さ
    fast_block_chars: int = 400

    # --- 詳細モード ---
    single_pass_tokens: int = 3500       # これ以下なら分割せず1回で要約
    chunk_tokens: int = 1500
    map_max_output: int = 280
    map_points: int = 4                  # 1つの部分から取り出す要点の最大数
    reduce_input_tokens: int = 3500
    reduce_max_output: int = 1000
    # 一般資料の詳細モードは、統合をモデルに書き直させず、部分要約の文をそのまま採用する（概要だけ短く生成）。
    # 小型モデルは統合で文を書き換えて原文に無い内容を混ぜ、参照番号も外すことが実測で分かったため。
    task_cards: bool = True              # 課題・手順を原文から抜き出す（モデルを使わない）
    extractive_reduce: bool = True
    extractive_max_items: int = 8        # 主要ポイントの上限の目安（30ページ以上は +2）
    overview_max_output: int = 300       # 概要（200〜350字）の分
    time_budget_s: float | None = 300.0  # 詳細モードの要約時間の目安（秒）。超えそうなら重要部分から処理。0で無制限

    drop_references: bool = True         # 論文の参考文献以降を要約対象から外す

    def validate(self) -> "Config":
        if self.host != LOOPBACK:
            raise ValueError(f"推論サーバーは {LOOPBACK} のみ許可されています（指定: {self.host}）")
        if self.threads < 1:
            raise ValueError("threads は1以上にしてください")
        margin = 700  # システムプロンプトと指示文の分
        checks = {
            "fast_input_tokens + fast_max_output": self.fast_input_tokens + self.fast_max_output,
            "chunk_tokens + map_max_output": self.chunk_tokens + self.map_max_output,
            "reduce_input_tokens + reduce_max_output": self.reduce_input_tokens + self.reduce_max_output,
            "single_pass_tokens + reduce_max_output": self.single_pass_tokens + self.reduce_max_output,
        }
        for name, total in checks.items():
            if total + margin > self.ctx_size:
                raise ValueError(
                    f"ctx_size={self.ctx_size} に対して {name}={total} が大きすぎます。"
                    "ctx_size を増やすか、入力・出力の上限を減らしてください。"
                )
        return self

    def with_overrides(self, **kw) -> "Config":
        return replace(self, **{k: v for k, v in kw.items() if v is not None}).validate()


def _model_matches(name: str, hint: str) -> bool:
    # "4b" が "14b" に一致しないよう、直前が数字・小数点でないことを要求する
    return re.search(rf"(?<![\d.]){re.escape(hint.lower())}", name.lower()) is not None


def resolve_model(cfg: Config, hint: str | None = None) -> Path:
    """`--model` の指定（パス or "1.7b" / "4b"）から GGUF ファイルを決める。"""
    if hint:
        p = Path(hint)
        if p.is_file():
            return p
    candidates = sorted(cfg.models_dir.glob("*.gguf")) if cfg.models_dir.is_dir() else []
    if hint:
        matched = [c for c in candidates if _model_matches(c.name, hint)]
        if matched:
            return matched[0]
        raise AssetNotFoundError(
            f"モデル '{hint}' が {cfg.models_dir} に見つかりません。"
            + (asset_hint("model") if is_frozen() else
               f"`python tools/setup_assets.py model {hint}` で取得するか、ファイルのパスを --model に指定してください。")
        )
    if not candidates:
        raise AssetNotFoundError(
            f"モデルファイル(.gguf)が {cfg.models_dir} にありません。" + asset_hint("model")
        )
    for pref in ("1.7b", "4b"):  # 既定は高速な 1.7B を優先
        for c in candidates:
            if _model_matches(c.name, pref):
                return c
    return candidates[0]


def resolve_server(cfg: Config, hint: str | None = None) -> Path:
    """llama-server の実行ファイルを探す。優先順: 引数 > 環境変数 > llama/ 配下 > PATH。"""
    tried: list[str] = []
    for raw in (hint, os.environ.get("LOCALDOC_LLAMA_SERVER")):
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
            tried.append(str(p))
    if cfg.llama_dir.is_dir():
        for p in cfg.llama_dir.rglob(SERVER_EXE):
            return p
    found = shutil.which("llama-server")
    if found:
        return Path(found)
    raise AssetNotFoundError(
        f"llama-server が見つかりません（{cfg.llama_dir} を確認しました）。"
        + (asset_hint("llama") if is_frozen() else "`python tools/setup_assets.py llama` で取得するか、--llama-server にパスを指定してください。")
        + (f" 指定されたパス: {', '.join(tried)}" if tried else "")
    )
