"""PDF → 整形 → 要約 までの一連の流れ。CLI・ベンチ・（将来の）GUIワーカーが共通で使う。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.cancel import CancelToken
from app.core.config import Config
from app.core.metrics import RunMetrics
from app.pdf.extractor import ExtractResult, extract_pdf
from app.pdf.text_processor import CleanResult, chunk_blocks, clean_blocks, estimate_tokens
from app.summary.schemas import SummaryResult
from app.summary.summarizer import ChatEngine, Progress, Summarizer


@dataclass
class Prepared:
    extraction: ExtractResult
    cleaned: CleanResult

    @property
    def warnings(self) -> list[str]:
        return self.extraction.warnings + self.cleaned.warnings


def prepare(pdf_path: str | Path, cfg: Config, metrics: RunMetrics | None = None) -> Prepared:
    """PDFの読み込みと整形（LLM不要）。"""
    ext = extract_pdf(
        pdf_path,
        max_mb=cfg.max_pdf_mb,
        max_pages=cfg.max_pages,
        max_chars_per_page=cfg.max_chars_per_page,
    )
    cleaned = clean_blocks(ext.blocks, drop_references=cfg.drop_references)
    if metrics is not None:
        metrics.pdf_load_s = ext.elapsed_s
    return Prepared(ext, cleaned)


def summarize(
    prepared: Prepared,
    cfg: Config,
    engine: ChatEngine,
    *,
    mode: str,
    kind: str = "auto",
    metrics: RunMetrics | None = None,
    progress: Progress | None = None,
    cancel: CancelToken | None = None,
    raw_hook=None,
) -> SummaryResult:
    doc_kind = prepared.cleaned.doc_kind if kind == "auto" else kind
    s = Summarizer(engine, cfg, metrics=metrics, progress=progress, cancel=cancel, raw_hook=raw_hook)
    return s.run(
        prepared.cleaned.blocks,
        mode=mode,
        kind=doc_kind,
        source_name=prepared.extraction.source_name,
        page_count=prepared.extraction.page_count,
        warnings=prepared.warnings,
    )


# このPCでの実測が無い段階の目安（i7-1360P・GPUなしでの実測値。起動後は実測値で置き換える）
DEFAULT_PROMPT_TPS = 55.0
DEFAULT_GEN_TPS = 15.0


def estimate_seconds(prepared: Prepared, cfg: Config, mode: str, *,
                     prompt_tps: float | None = None, gen_tps: float | None = None) -> float:
    """要約にかかる時間の概算（秒）。モデルの起動時間は含めない。画面に出す「目安」用で、正確ではない。"""
    ptps = prompt_tps or DEFAULT_PROMPT_TPS
    gtps = gen_tps or DEFAULT_GEN_TPS
    blocks = prepared.cleaned.blocks
    total = sum(estimate_tokens(b.text) + 6 for b in blocks)
    if mode == "fast":
        eval_tok = 0.82 * min(total, cfg.fast_input_tokens) + 450          # 入力の見積り→実測の比 + 指示文
        gen_tok = 520                                                       # 概要（200〜350字）＋主要ポイント5〜8項目
    elif total <= cfg.single_pass_tokens:                                   # 短い資料: 分割せず1回で、概要・主要ポイント・詳細・注意点を書く
        eval_tok = 0.88 * total + 700
        gen_tok = 750
    else:
        n_chunks = max(1, len(chunk_blocks(blocks, cfg.chunk_tokens)))
        eval_tok = 0.88 * total + 200 * n_chunks + 900
        gen_tok = 160 * n_chunks + 330                                      # 部分ごとの要点 + 概要
    sec = eval_tok / ptps + gen_tok / gtps
    if mode != "fast" and cfg.time_budget_s > 0:
        sec = min(sec, cfg.time_budget_s + 40)                         # 時間予算を超えそうなら、重要部分から処理して打ち切る
    return sec
