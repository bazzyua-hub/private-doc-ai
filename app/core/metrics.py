"""性能計測（企画書 8.1）。

PDF読み込み・モデル起動・入力処理速度・出力速度・総処理時間・RAM を分けて記録する。
文書の内容は一切含めない（S-02）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CallMetrics:
    label: str
    prompt_tokens: int          # プロンプト全体のトークン数（キャッシュ再利用分を含む）
    prompt_eval_tokens: int     # 実際に評価したトークン数（速度計算に使う）
    completion_tokens: int
    prompt_s: float             # プロンプト評価に要した時間
    gen_s: float                # 生成に要した時間
    wall_s: float               # 呼び出し全体の実時間
    est_prompt_tokens: int = 0  # 事前の見積り（見積り精度の確認用）


@dataclass
class RunMetrics:
    pdf_load_s: float = 0.0
    model_start_s: float = 0.0
    summarize_s: float = 0.0
    peak_rss_mb: float | None = None
    calls: list[CallMetrics] = field(default_factory=list)

    def add_call(self, call: CallMetrics) -> None:
        self.calls.append(call)

    @property
    def prompt_eval_tokens(self) -> int:
        return sum(c.prompt_eval_tokens for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def prompt_tps(self) -> float | None:
        secs = sum(c.prompt_s for c in self.calls)
        return self.prompt_eval_tokens / secs if secs > 0 else None

    @property
    def gen_tps(self) -> float | None:
        secs = sum(c.gen_s for c in self.calls)
        return self.completion_tokens / secs if secs > 0 else None

    @property
    def token_estimate_ratio(self) -> float | None:
        """実測トークン数 / 見積りトークン数。1より大きいと見積りが甘い。"""
        est = sum(c.est_prompt_tokens for c in self.calls)
        act = sum(c.prompt_tokens for c in self.calls)
        return act / est if est > 0 and act > 0 else None

    def report_lines(self) -> list[str]:
        def f(v: float | None, unit: str) -> str:
            return "計測不可" if v is None else f"{v:,.1f} {unit}"

        lines = [
            f"PDF読み込み     : {self.pdf_load_s:.2f} 秒",
            f"モデル起動      : {self.model_start_s:.2f} 秒（要約時間には含めない）",
            f"要約処理        : {self.summarize_s:.1f} 秒（LLM呼び出し {len(self.calls)} 回）",
            f"入力処理速度    : {f(self.prompt_tps, 'tokens/s')}（評価 {self.prompt_eval_tokens:,} tokens）",
            f"出力速度        : {f(self.gen_tps, 'tokens/s')}（生成 {self.completion_tokens:,} tokens）",
            f"サーバーRAM最大 : {f(self.peak_rss_mb, 'MB')}",
        ]
        ratio = self.token_estimate_ratio
        if ratio is not None:
            lines.append(f"トークン見積り  : 実測は見積りの {ratio:.2f} 倍")
        return lines

    def to_dict(self) -> dict:
        return {
            "pdf_load_s": self.pdf_load_s,
            "model_start_s": self.model_start_s,
            "summarize_s": self.summarize_s,
            "prompt_tps": self.prompt_tps,
            "gen_tps": self.gen_tps,
            "prompt_eval_tokens": self.prompt_eval_tokens,
            "completion_tokens": self.completion_tokens,
            "peak_rss_mb": self.peak_rss_mb,
            "calls": len(self.calls),
        }
