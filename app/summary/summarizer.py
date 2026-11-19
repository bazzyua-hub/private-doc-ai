"""要約の本体（F-03 / F-04 / F-05 / 5.2）。

高速モード : 重要ブロックだけを選び、1回のLLM呼び出しで要約する。
詳細モード : 全文を分割→各チャンクを要約（map）→統合（reduce）する階層的要約。

参照の扱い（幻覚対策）
- map の各要点は、そのチャンクのブロックIDだけを引用できる（範囲外の番号は捨てる）
- reduce では部分要約を [S番号] で引用させ、元のブロックIDへの対応はプログラム側で保持する
  （モデルにページ番号やブロックIDを「思い出させて」出力させない）
- 最終出力の主張ごとに、参照の存在確認・内容の照合・数値の実在確認を行い、原文で確認できない記述は表示しない
  （「確認できない」ものを推測で補わない）。概要の文も、モデルに渡した要点の一覧で説明できるかを検算する
- 「やること・手順」はモデルに書かせず、原文の指示文をプログラムが抜き出す（tasks.py）。指示が無ければ「該当なし」

LLM エンジンは `chat(system, user, *, max_tokens, cancel)` を持つ任意のオブジェクトでよい
（テストでは偽エンジンを差し込む）。
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import replace
from typing import Callable, Protocol

from app.core.cancel import CancelToken
from app.core.config import Config
from app.core.errors import ContextOverflowError, SummaryError
from app.core.metrics import CallMetrics, RunMetrics
from app.llm import prompts
from app.llm.engine import ChatResult
from app.llm.prompts import Item
from app.pdf.extractor import Block
from app.pdf.text_processor import (
    Chunk,
    block_priority,
    chunk_blocks,
    chunk_priority,
    estimate_tokens,
    select_for_fast,
    split_to_limit,
)
from .attribution import Attributor
from .fidelity import FidelityChecker
from .citations import (
    _NO_REF_OK, check_numbers, clean_model_output, find_simplified, is_vacuous, lines_to_items, parse_refs,
    resolve_sections, split_sections,
)
from .schemas import STATUS_INFERRED, STATUS_OK, STATUS_OVERALL, STATUS_RANGE, STATUS_UNRESOLVED, Claim, Section, SummaryResult
from .tasks import TaskExtraction, extract_instructions, extract_task_cards

MIN_SUPPORT = 0.5            # 要点が、引用先の本文で説明できる割合の下限（実データで、原文どおりの要点は0.58以上、原文に無い枠組みを足した要点は0.44以下）
MIN_OVERVIEW_COVER = 0.25    # 概要の1文が、要点の一覧で説明できる割合の下限（一覧にある内容は0.58以上、作り話は0.15以下）
MIN_OVERVIEW_TERMS = 0.6     # 概要の1文の専門用語・固有名詞のうち、要点の一覧に見つかる割合の下限（作り話は0.5以下）
OVERVIEW_MIN_CHARS = 150      # 検証で文が落ちて、概要がこれより短くなったときは、確認済みの主要ポイントで補う（上限は OVERVIEW_MAX_CHARS）
OVERVIEW_MAX_CHARS = 380     # 概要の長さの上限の目安（超えたら、後ろの文から外す。200字未満でも足さない）

Progress = Callable[[str, int, int], None]


class ChatEngine(Protocol):
    def chat(self, system: str, user: str, *, max_tokens: int, cancel: CancelToken | None = None) -> ChatResult: ...


class Summarizer:
    def __init__(
        self,
        engine: ChatEngine,
        cfg: Config,
        *,
        metrics: RunMetrics | None = None,
        progress: Progress | None = None,
        cancel: CancelToken | None = None,
        clock: Callable[[], float] = time.perf_counter,
        raw_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        self.engine = engine
        self.cfg = cfg
        self.metrics = metrics or RunMetrics()
        self.progress = progress or (lambda stage, done, total: None)
        self.cancel = cancel or CancelToken()
        self.clock = clock
        self.raw_hook = raw_hook          # 診断用: モデルの生の出力を受け取る（既定では何も保存・表示しない）
        self.warnings: list[str] = []
        self._page_texts: dict[int, list[str]] = {}
        self._by_id: dict[int, Block] = {}
        self._doc_text = ""
        self._attributor: Attributor | None = None
        self._tasks: TaskExtraction = TaskExtraction()
        self._card_ids: set[int] = set()
        self._instr: list[Claim] = []
        self._dropped = 0                  # 原文で確認できなかったため、表示しなかった記述の数
        self._drop_reasons: dict[str, int] = {}
        self._fidelity: FidelityChecker | None = None
        self._replaced = 0                 # 意味がずれる恐れがあったため、原文の文に置き換えた記述の数

    # ------------------------------------------------------------ 入口

    def run(
        self,
        blocks: list[Block],
        *,
        mode: str,
        kind: str = "general",
        source_name: str = "",
        page_count: int = 0,
        warnings: list[str] | None = None,
    ) -> SummaryResult:
        if not blocks:
            raise SummaryError("要約できる本文がありません。")
        if mode not in ("fast", "detail"):
            raise SummaryError(f"未対応のモード: {mode}")
        self.warnings = list(warnings or [])
        self._by_id = {b.id: b for b in blocks}
        self._page_texts = {}
        for b in blocks:
            self._page_texts.setdefault(b.page, []).append(b.text)
        self._doc_text = "\n".join(b.text for b in blocks)
        self._attributor = Attributor(blocks)
        self._fidelity = FidelityChecker(blocks, self._attributor)
        self._dropped = 0
        self._drop_reasons = {}
        self._replaced = 0
        self._tasks = extract_task_cards(blocks) if (self.cfg.task_cards and kind != "paper") else TaskExtraction()
        self._card_ids = self._tasks.block_ids()
        self._instr = extract_instructions(blocks, exclude_ids=self._card_ids)
        if self._tasks.missing:
            self.warnings.append(
                "資料の一覧にある " + "・".join(self._tasks.missing) + " は、指示文が見つかりませんでした"
                "（この資料に本文が無い可能性があります）。"
            )
        if self._tasks.skipped_pages:
            from .schemas import format_pages
            self.warnings.append(
                f"{format_pages(self._tasks.skipped_pages)} の番号つきの手順は、読者への作業指示とは書かれていない"
                "（実験・方法の説明などの可能性がある）ため、「やること・手順」には入れていません。")
        t0 = self.clock()
        self._t0 = t0

        if mode == "fast":
            sections = self._run_fast(blocks, kind)
        else:
            sections = self._run_detail(blocks, kind)

        self.metrics.summarize_s = self.clock() - t0
        if self._dropped:
            why = "、".join(f"{k}{n}件" for k, n in sorted(self._drop_reasons.items(), key=lambda kv: -kv[1]))
            self.warnings.append(
                f"原文との照合で内容を確認できなかった記述が{self._dropped}件あったため、表示していません（内訳：{why}）。")
        if self._replaced:
            self.warnings.append(
                f"{self._replaced}件の記述は、原文と意味がずれる恐れ（数値が指す対象・条件・限定・否定、定義の言い換えなど）があったため、"
                "原文の文にそのまま置き換えました。")
        cited = ({i for s in sections for c in s.claims for i in c.block_ids} | self._card_ids
                 | {i for c in self._instr for i in c.block_ids})
        return SummaryResult(
            mode=mode,
            doc_kind=kind,
            source_name=source_name,
            page_count=page_count or max(b.page for b in blocks),
            sections=sections,
            warnings=self.warnings,
            evidence={i: self._by_id[i] for i in sorted(cited) if i in self._by_id},
            metrics=self.metrics,
            tasks=self._tasks.cards,
            task_outline=self._tasks.outline,
            instructions=self._instr,
            unverified_dropped=self._dropped,
            fidelity_replaced=self._replaced,
        )

    # ------------------------------------------------------------ 高速モード

    def _run_fast(self, blocks: list[Block], kind: str) -> list[Section]:
        budget = self.cfg.fast_input_tokens
        for attempt in range(3):
            selected = select_for_fast(blocks, budget_tokens=budget, block_chars=self.cfg.fast_block_chars)
            if not selected:
                raise SummaryError("要約対象のブロックを選べませんでした。")
            items = [Item(f"B{b.id}", b.text, {b.id}) for b in selected]
            user = prompts.final_prompt(items, kind=kind, preface=prompts.PREFACE_FAST, example="B3", lean=True)
            self.progress("fast", 0, 1)
            try:
                res = self._call("fast", user, self.cfg.fast_max_output)
            except ContextOverflowError:
                budget = int(budget * 0.6)
                continue
            self.progress("fast", 1, 1)
            pages = sorted({b.page for b in selected})
            total_tok = sum(estimate_tokens(b.text) + 6 for b in blocks)
            read_tok = sum(estimate_tokens(b.text) + 6 for b in selected)
            share = read_tok / total_tok if total_tok else 1.0
            self.warnings.append(
                f"高速モード: 重要そうな部分（{len(selected)}/{len(blocks)}ブロック、本文の約{share * 100:.0f}%）だけを抜粋して要約しています。"
                "全文は読んでいません。"
            )
            hint = self._detail_estimate_hint(blocks, total_tok) if share < 0.5 else ""
            if hint:
                self.warnings.append(hint)
            if len(pages) < len({b.page for b in blocks}):
                self.warnings.append("抜粋に含まれなかったページがあります。詳細モードで全体を確認できます。")
            return self._finalize(self._usable_text(res), items, "B")
        raise SummaryError("入力がコンテキスト長に収まりませんでした。ctx_size を増やしてください。")

    def _detail_estimate_hint(self, blocks: list[Block], total_tok: int) -> str:
        """この実行で測った速度から、詳細モードの所要時間を見積もって知らせる。"""
        ptps, gtps = self.metrics.prompt_tps, self.metrics.gen_tps
        if not ptps or not gtps:
            return ""
        n_chunks = max(1, len(chunk_blocks(blocks, self.cfg.chunk_tokens)))
        eval_tok = 0.88 * total_tok + 200 * n_chunks + 900          # 実測（27ページ資料）に合わせた概算
        gen_tok = 160 * n_chunks + 100
        minutes = (eval_tok / ptps + gen_tok / gtps) / 60
        return (
            f"詳細モードなら全体を読めます（この実行の速度からの概算で約{minutes:.1f}分）。"
            "ページ数が多く文字の少ない資料（スライド等）では、詳細モードの方が向いています。"
        )

    # ------------------------------------------------------------ 詳細モード

    def _run_detail(self, blocks: list[Block], kind: str) -> list[Section]:
        total = sum(estimate_tokens(b.text) + 6 for b in blocks)
        if total <= self.cfg.single_pass_tokens:
            items = [Item(f"B{b.id}", b.text, {b.id}) for b in blocks]
            user = prompts.final_prompt(items, kind=kind, preface=prompts.PREFACE_FULL, example="B3")
            self.progress("final", 0, 1)
            res = self._call("single", user, self.cfg.reduce_max_output)
            self.progress("final", 1, 1)
            return self._finalize(self._usable_text(res), items, "B")

        chunks = chunk_blocks(blocks, self.cfg.chunk_tokens)
        summaries = self._map_stage(chunks, blocks)
        if not summaries:
            raise SummaryError("各部分から要点を取り出せませんでした。")
        if self.cfg.extractive_reduce and kind != "paper":
            return self._extractive_final(summaries, blocks)
        items = self._reduce_stage(summaries)
        user = prompts.final_prompt(items, kind=kind, preface=prompts.PREFACE_REDUCE, example="S2")
        self.progress("final", 0, 1)
        res = self._call("reduce", user, self.cfg.reduce_max_output)
        self.progress("final", 1, 1)
        return self._finalize(self._usable_text(res), items, "S")

    def _map_stage(self, chunks: list[Chunk], blocks: list[Block]) -> list[Item]:
        budget = self.cfg.time_budget_s
        order = list(range(len(chunks)))
        if budget:
            scores = block_priority(blocks)
            order.sort(key=lambda i: -chunk_priority(chunks[i], scores, len(chunks)))
        done: dict[int, list[Item]] = {}
        durations: list[float] = []
        skipped: list[int] = []
        for k, idx in enumerate(order):
            if budget and durations:
                avg = statistics.fmean(durations)
                # 残りの map 1件 + 統合（map 1件の1.5倍と見積る）が予算内に収まらなければ打ち切る
                if (self.clock() - self._t0) + avg + 1.5 * avg > budget:
                    skipped = order[k:]
                    break
            self.cancel.raise_if_set()
            self.progress("map", k, len(chunks))
            t = self.clock()
            done[idx] = self._map_chunk(chunks[idx])
            durations.append(self.clock() - t)
        self.progress("map", len(done), len(chunks))
        if skipped:
            pages = sorted({p for i in skipped for p in range(chunks[i].pages[0], chunks[i].pages[1] + 1)})
            self.warnings.append(
                f"時間予算（{budget:.0f}秒）に収めるため、{len(skipped)}/{len(chunks)}個の部分を処理していません"
                f"（該当ページの目安: {_page_span(pages)}）。重要度の低い部分から省いています。"
            )
        items: list[Item] = []
        for idx in sorted(done):
            items.extend(done[idx])
        return _relabel(items)

    def _map_chunk(self, chunk: Chunk, depth: int = 0) -> list[Item]:
        items = [Item(f"B{b.id}", b.text, {b.id}) for b in chunk.blocks]
        user = prompts.map_prompt(items, heading=chunk.heading, pages=chunk.pages, max_points=self.cfg.map_points)
        try:
            res = self._call("map", user, self.cfg.map_max_output)
        except ContextOverflowError:
            # 入力が長すぎた場合は半分に分けてやり直す（1ブロックだけなら文の境界で分ける）
            if depth >= 2:
                raise
            if len(chunk.blocks) >= 2:
                half = len(chunk.blocks) // 2
                parts = [chunk.blocks[:half], chunk.blocks[half:]]
            else:
                only = chunk.blocks[0]
                pieces = split_to_limit(only.text, max(50, estimate_tokens(only.text) // 2 + 10))
                if len(pieces) < 2:
                    raise
                parts = [[replace(only, text=pc)] for pc in pieces]
            out: list[Item] = []
            for part in parts:
                sub = Chunk(chunk.index, part, sum(estimate_tokens(b.text) for b in part), chunk.heading)
                out.extend(self._map_chunk(sub, depth + 1))
            return out

        ids = chunk.ids
        result: list[Item] = []
        raw_items = lines_to_items(clean_model_output(res.text).splitlines(), join_paragraphs=False)
        if res.finish_reason == "length" and raw_items:
            raw_items = raw_items[:-1]                  # 上限で途中切れになった最後の1件は信用できないので捨てる
        for raw in raw_items:
            text, nums, _ = parse_refs(raw, "B")
            if not text or text.rstrip("。.") == "要点なし":
                continue
            if _is_code_echo(text) or _DIRECTIVE_END.search(text):
                continue                                        # コマンド行の写し・作業指示の文は要点にしない（指示は原文から抜き出す）
            item = self._verified_item(text, {n for n in nums if n in ids}, chunk)       # このチャンク外の番号は捨てる
            if item is None:
                self._drop("根拠を原文で見つけられない要点")                # 原文で確認できない要点は使わない
                continue
            result.append(item)
        return result

    def _verified_item(self, text: str, cited: set[int], chunk: Chunk) -> Item | None:
        """モデルの要点を原文と照合し、根拠のブロックを決める。確認できなければ None（使わない）。

        1. 引用した番号が、その要点の内容を実際に説明しているか（説明できなければ引用を信用しない）
        2. 引用が無い・信用できないときは、本文との照合で根拠のブロックを探す
        3. 番号が無く、見つからなくても、このチャンク全体（のページ）で十分に説明できるなら、ページの範囲だけを示して使う
        4. それでも説明できなければ、原文で確認できない内容として使わない（番号を付けたのに内容が合わない要点も含む）
        """
        att = self._attributor
        if att is None:
            return Item("", text, cited or set(chunk.ids), coarse=not cited, pages=chunk.pages)
        item: Item | None = None
        if cited and att.support(text, cited) >= MIN_SUPPORT:
            item = Item("", text, cited, coarse=False, pages=self._pages_of(cited))
        else:
            inferred = att.attribute(text)
            inferred = [i for i in inferred if i in chunk.ids]
            if inferred:
                item = Item("", text, set(inferred), coarse=False, pages=self._pages_of(set(inferred)))
            elif not cited and att.support(text, chunk.ids) >= MIN_SUPPORT:
                item = Item("", text, set(chunk.ids), coarse=True, pages=chunk.pages)
        if item is None:
            return None                                             # 番号を付けたのに内容が合わない要点は、ずれた言い換えとみなして使わない
        gated = self._fidelity_gate(item.text, item.ids)
        if gated is None:
            return None
        gated = self._expand_fragment(gated, item.ids)
        gated = self._repair_head(gated, item.ids)
        gated = self._pin_definition(gated, item.ids)
        if gated != item.text:
            item = replace(item, text=gated)
        return item

    def _pin_definition(self, text: str, ids: set[int] | list[int]) -> str:
        """「〜とは…を意味する」のような定義の文は、言い換えると意味が変わりやすい（実PDFで、「自らは受け入れていない」「と信じている」が
        抜けた）。原文の文をそのまま使う。見つからなければ、そのまま返す（言い換えのずれは別の検査で見る）。"""
        fc = self._fidelity
        if fc is None or not ids or not _DEFINITION.search(text):
            return text
        fixed = fc.restore(text, ids)
        if not fixed or fixed.rstrip("。") == text.rstrip("。") or not _DEFINITION.search(fixed):
            return text
        self._replaced += 1
        return fixed

    def _pin_overview_definition(self, sent: str, sources: list[str]) -> str | None:
        """概要の定義の文は、要点の一覧にある原文どおりの定義の文に置き換える。対応する要点が無ければ None。"""
        from .attribution import _bigrams
        want = _bigrams(sent)
        best, score = None, 0.0
        for src in sources:
            if not _DEFINITION.search(src):
                continue
            bg = _bigrams(src)
            sc = len(bg & want) / max(1, min(len(bg), len(want)))
            if sc > score:
                best, score = src, sc
        if best is None or score < 0.5:
            return None
        return best.rstrip("。.") + "。"

    def _drop(self, reason: str) -> None:
        """表示しない記述を数える。内訳（理由別）も数えて、どこが厳しすぎるか分かるようにする。"""
        self._dropped += 1
        self._drop_reasons[reason] = self._drop_reasons.get(reason, 0) + 1

    def _repair_head(self, text: str, ids: set[int] | list[int]) -> str:
        """文頭が「が」「を」などの助詞で始まる要点は、前のページから続く文の後半。前のブロックの文頭側（主語）を補う。

        補えない（前のブロックが無い・長すぎる）ときは、そのまま返す。
        """
        t = text.strip()
        if not _HEAD_PARTICLE.match(t):
            return text
        probe = _flat_text(t)[:10]
        for i in sorted(ids):
            blk = self._by_id.get(i)
            if blk is None or probe not in _flat_text(blk.text):
                continue
            prev = None
            for back in range(1, 5):                                           # 間に挟まるページ番号・ヘッダ（短い・数字だけ・誌名）は飛ばす
                cand = self._by_id.get(i - back)
                if cand is None:
                    break
                flat = _flat_text(cand.text)
                if len(flat) >= 16 and not _PAGE_FURNITURE.search(flat):
                    prev = cand
                    break
            if prev is None:
                continue
            tail = re.split(r"[。．.！？]", _flat_text(prev.text).rstrip())[-1].strip()
            if 2 <= len(tail) <= 40 and not _HEAD_PARTICLE.match(tail):
                m = re.match(r"^[^、，]{0,12}[、，]", tail)
                if m and len(tail) - m.end() >= 4:
                    tail = tail[m.end():]                                      # 「本モデルでは、」のような前置きは外す
                return tail + t
        return text

    def _expand_fragment(self, text: str, ids: set[int] | list[int]) -> str:
        """「〜こと」「〜点」で終わる断片は、原文の1文（そのまま）に広げる。広げられなければそのまま。"""
        fc = self._fidelity
        if fc is None or not ids or not _FRAGMENT.search(text.strip()):
            return text
        fixed = fc.restore(text, ids)
        return fixed if fixed and len(fixed) > len(text) else text

    def _fidelity_gate(self, text: str, ids: set[int] | list[int]) -> str | None:
        """数値の対象・条件・限定などが原文と食い違う記述を、原文の文に置き換える。置き換えられなければ None（使わない）。"""
        fc = self._fidelity
        if fc is None or not ids or not fc.check(text, ids):
            return text
        fixed = fc.restore(text, ids)
        if fixed is None:
            self._drop("意味のずれを原文の文で直せなかった要点")
            return None
        self._replaced += 1
        return fixed

    def _extractive_final(self, summaries: list[Item], blocks: list[Block]) -> list[Section]:
        """部分要約の文をそのまま主要ポイントにし、概要だけ短く生成する（書き換えによる誤りを避ける）。

        参照（ページ）は部分要約の段階でプログラムが検証済みのものがそのまま使える。
        重複する文は落とし、多すぎる場合は重要度の高い箇所のものを、文書の前半から後半まで偏らないように残す。
        """
        usable = [it for it in summaries if not is_vacuous(it.text)] or summaries      # 中身のない項目は使わない
        items = _dedupe_items(usable)
        scores = block_priority(blocks)
        # 課題カード・作業指示の文と同じ箇所だけを根拠にした要点は、それらと重複するので省く
        covered = self._card_ids | {i for c in self._instr for i in c.block_ids}
        if covered:
            rest = [it for it in items if not (it.ids and it.ids <= covered)]
            items = rest or items
        cap = self._points_cap(blocks)
        if len(items) > cap:
            items = _pick_spread(items, cap, scores)
        items = _relabel(items)
        self.progress("final", 0, 1)
        first_page = min((b.page for b in blocks), default=1)
        title = " ".join(b.text for b in blocks if b.page == first_page and b.is_heading)[:80]    # 資料のタイトル（概要の手がかり）
        tasks = self._task_lines()
        res = self._call("overview", prompts.overview_prompt([it.text for it in items], title, tasks),
                         self.cfg.overview_max_output)
        self.progress("final", 1, 1)
        overview = " ".join(
            ln.strip().lstrip("-*・# ").strip() for ln in self._usable_text(res).splitlines()
            if ln.strip() and not re.match(r"^\s*#{1,6}\s*概要\s*$", ln)
        ).strip()
        parts: list[str] = []
        if overview:
            parts.append("## 概要\n- " + overview)
        if items:
            parts.append("## 主要ポイント\n" + "\n".join(f"- {it.text} [{it.label}]" for it in items))
        return self._finalize("\n\n".join(parts), items, "S", overview_sources=[title, *tasks, *(it.text for it in items)])

    def _points_cap(self, blocks: list[Block]) -> int:
        """主要ポイントの数の上限。長い資料では少し増やす（内容が少なければ、上限まで埋めない）。"""
        pages = max((b.page for b in blocks), default=1)
        return self.cfg.extractive_max_items + (2 if pages >= 30 else 0)

    def _reduce_stage(self, items: list[Item]) -> list[Item]:
        limit = self.cfg.reduce_input_tokens
        level = 0
        while _items_tokens(items) > limit and level < 3:
            level += 1
            groups: list[list[Item]] = []
            cur: list[Item] = []
            used = 0
            for it in items:
                c = estimate_tokens(it.text) + 14
                if cur and used + c > limit * 0.8:
                    groups.append(cur)
                    cur, used = [], 0
                cur.append(it)
                used += c
            if cur:
                groups.append(cur)
            merged: list[Item] = []
            for gi, g in enumerate(groups):
                self.cancel.raise_if_set()
                self.progress("reduce", gi, len(groups))
                merged.extend(self._collapse(g))
            shrank = _items_tokens(merged) < _items_tokens(items)
            items = _relabel(merged)
            if not shrank:                                      # 縮まないなら無限に繰り返さない
                break
        if _items_tokens(items) > limit:
            kept: list[Item] = []
            used = 0
            for it in items:
                c = estimate_tokens(it.text) + 14
                if used + c > limit:
                    break
                kept.append(it)
                used += c
            self.warnings.append("部分要約が長すぎたため、後半の一部を最終統合に含めていません。")
            items = kept
        return items

    def _collapse(self, group: list[Item]) -> list[Item]:
        user = prompts.collapse_prompt(group, max_points=max(3, len(group) // 2))
        res = self._call("collapse", user, self.cfg.map_max_output * 2)
        by_n = {int(it.label[1:]): it for it in group}
        out: list[Item] = []
        raw_items = lines_to_items(clean_model_output(res.text).splitlines(), join_paragraphs=False)
        if res.finish_reason == "length" and raw_items:
            raw_items = raw_items[:-1]
        for raw in raw_items:
            text, nums, _ = parse_refs(raw, "S")
            if not text:
                continue
            valid = [n for n in nums if n in by_n]
            if not valid:
                ids = {i for it in group for i in it.ids}
                out.append(Item("", text, ids, coarse=True, pages=_union_pages(group, self)))
                continue
            ids = {i for n in valid for i in by_n[n].ids}
            coarse = all(by_n[n].coarse for n in valid)
            out.append(Item("", text, ids, coarse=coarse, pages=self._pages_of(ids)))
        return out or [replace(it, label="") for it in group]

    # ------------------------------------------------------------ 共通

    def _call(self, label: str, user: str, max_tokens: int) -> ChatResult:
        self.cancel.raise_if_set()
        est = estimate_tokens(prompts.SYSTEM) + estimate_tokens(user)
        t = self.clock()
        res = self.engine.chat(prompts.SYSTEM, user, max_tokens=max_tokens, cancel=self.cancel)
        wall = self.clock() - t
        if self.raw_hook:
            self.raw_hook(label, res.text)
        if res.finish_reason == "length":
            msg = "出力が上限に達して途中で打ち切られた箇所があります。"
            if msg not in self.warnings:
                self.warnings.append(msg)
        self.metrics.add_call(
            CallMetrics(label, res.prompt_tokens, res.prompt_eval_tokens, res.completion_tokens,
                        res.prompt_s, res.gen_s, wall, est)
        )
        return res

    @staticmethod
    def _usable_text(res: ChatResult) -> str:
        """出力が上限で途中切れなら、最後の（途中で終わった）行を捨てる。"""
        if res.finish_reason == "length" and "\n" in res.text.rstrip():
            return res.text.rstrip().rsplit("\n", 1)[0]
        return res.text

    def _finalize(self, text: str, items: list[Item], prefix: str, *, overview_sources: list[str] | None = None) -> list[Section]:
        raw = split_sections(clean_model_output(text))
        if not raw:
            self.warnings.append("モデルの出力が指定した形式になっていなかったため、そのまま表示しています。")
            raw = [("要約", clean_model_output(text).splitlines())]
        allowed = {int(it.label[1:]): set(it.ids) for it in items}
        coarse = {int(it.label[1:]) for it in items if it.coarse}
        sections = resolve_sections(
            raw, prefix=prefix, allowed=allowed, coarse=coarse,
            source_of=self._source_of, document_text=self._doc_text,
            attribute=self._attributor.attribute if self._attributor else None,
            support=self._attributor.support if self._attributor else None,
            min_support=MIN_SUPPORT,
        )
        # 「やること・手順」はモデルに書かせない（原文の指示文だけを、プログラムが抜き出す）。モデルが書いても使わない
        sections = [sec for sec in sections if sec.title != "やること・手順"]
        had_overview = any(sec.title == "概要" and sec.claims for sec in sections)
        sections = [self._verify_section(sec, overview_sources if overview_sources is not None else [it.text for it in items])
                    for sec in sections]
        sections = [sec for sec in sections if sec.claims]
        if not any(s.claims for s in sections):
            raise SummaryError("モデルの出力から要約を取り出せませんでした。原文との照合で確認できた記述がありません。")
        if had_overview and not any(sec.title == "概要" for sec in sections):
            fallback = self._overview_from_points(sections)
            if fallback is not None:
                sections.insert(0, fallback)
        self._extend_short_overview(sections)
        odd = find_simplified("".join(c.text for s in sections for c in s.claims))
        if odd:
            self.warnings.append(
                "中国語（簡体字）の字形が混ざっています（" + " ".join(odd) + "）。モデルの癖によるもので、"
                "該当箇所は読み替えてください。"
            )
        return sections

    def _verify_section(self, sec: Section, overview_sources: list[str]) -> Section:
        """原文で確認できない記述を落とす（推測で補わない）。落とした数は self._dropped に数える。"""
        if sec.title == "概要":
            return self._verify_overview(sec, overview_sources)
        kept: list[Claim] = []
        for c in sec.claims:
            if _NO_REF_OK.match(c.text):
                kept.append(c)                                  # 「記載なし」「特になし」
            elif c.status == STATUS_UNRESOLVED:
                self._drop("根拠を原文で見つけられない要点")                # 参照先を特定できず、本文とも照合できない
            elif _DIRECTIVE_END.search(c.text):
                self._drop("作業指示の形の文")                          # 作業指示の形の文は要点にしない（指示は原文から抜き出す）
            elif check_numbers(c.text, self._doc_text):
                self._drop("原文に無い数値を含む記述")                   # 資料のどこにも無い数値を含む
            elif c.status in (STATUS_OK, STATUS_INFERRED, STATUS_RANGE) and c.block_ids:
                gated = self._fidelity_gate(c.text, c.block_ids)    # 意味がずれていれば、原文の文に置き換える（できなければ使わない）
                if gated is not None:
                    gated = self._expand_fragment(gated, c.block_ids)
                    gated = self._repair_head(gated, c.block_ids)
                    gated = self._pin_definition(gated, c.block_ids)
                    kept.append(c if gated == c.text else replace(c, text=gated))
            else:
                kept.append(c)
        return Section(sec.title, kept)

    def _extend_short_overview(self, sections: list[Section]) -> None:
        """検証で概要の文が落ちて短くなりすぎたとき、確認済みの主要ポイント（結果・結論を優先）を足す。新しい文は作らない。"""
        ov = next((s for s in sections if s.title == "概要" and s.claims), None)
        pts = next((s for s in sections if s.title == "主要ポイント"), None)
        if ov is None or pts is None or len(pts.claims) < 5 or len(ov.claims[0].text) >= OVERVIEW_MIN_CHARS:
            return
        from .attribution import _bigrams
        text = ov.claims[0].text
        added = 0
        cands = [c for c in pts.claims if c.status in (STATUS_OK, STATUS_INFERRED, STATUS_RANGE) and not _NO_REF_OK.match(c.text)]
        for c in sorted(cands, key=lambda c: not _FINDING.search(c.text)):
            if len(text) >= 200:
                break
            piece = c.text.rstrip("。.") + "。"
            bg, have = _bigrams(piece), _bigrams(text)
            if len(text) + len(piece) > OVERVIEW_MAX_CHARS or (bg and len(bg & have) / len(bg) >= 0.5):
                continue
            text += piece
            added += 1
        if added:
            ov.claims[0] = Claim(text=text, status=STATUS_OVERALL)
            self.warnings.append("概要の文の一部を原文との照合で確認できなかったため、確認済みの主要ポイントを概要に補いました。")

    def _overview_from_points(self, sections: list[Section]) -> Section | None:
        """概要の文がすべて原文で確認できなかったときの代わり: 確認済みの主要ポイント（なければ詳細）の先頭を並べる。"""
        for title in ("主要ポイント", "詳細"):
            sec = next((x for x in sections if x.title == title), None)
            if sec is None:
                continue
            texts: list[str] = []
            for c in sec.claims:
                if _NO_REF_OK.match(c.text) or c.status == STATUS_UNRESOLVED:
                    continue
                piece = c.text.rstrip("。.") + "。"
                if texts and len("".join(texts)) + len(piece) > OVERVIEW_MAX_CHARS:
                    break
                texts.append(piece)
                if len(texts) >= 3:
                    break
            if texts:
                self.warnings.append("概要の文は原文との照合で確認できなかったため、確認できた主要ポイントから構成しました。")
                return Section("概要", [Claim(text="".join(texts), status=STATUS_OVERALL)])
        return None

    @staticmethod
    def _overview_supported(att: Attributor, sentence: str, sources: list[str]) -> bool:
        """概要の1文が、要点の一覧で説明できるか。言葉の重なりと、専門用語・固有名詞の裏付けの両方を見る。

        言葉の重なりだけだと、「〜を扱っている」のような一般的な言い回しの文を落としてしまい、
        逆に、実在の用語に作り話を混ぜた文は通ってしまう（実データで確認）。
        """
        if att.coverage_in(sentence, sources) < MIN_OVERVIEW_COVER:
            return False
        ratio, _missing = att.term_support(sentence, sources)
        return ratio is None or ratio >= MIN_OVERVIEW_TERMS

    def _verify_overview(self, sec: Section, sources: list[str]) -> Section:
        """概要の各文が、モデルに渡した要点の一覧で説明できるかを確かめる。説明できない文（足された内容）は落とす。"""
        att = self._attributor
        kept: list[str] = []
        for claim in sec.claims:
            for sent in _sentences(claim.text):
                pinned = self._pin_overview_definition(sent, sources) if _DEFINITION.search(sent) else None
                if pinned is not None:
                    if pinned not in kept:
                        kept.append(pinned)                     # 定義は言い換えず、要点にある原文どおりの文にする
                elif check_numbers(sent, self._doc_text):
                    self._drop("原文に無い数値を含む記述")                   # 資料のどこにも無い数値
                elif self._fidelity is not None and self._fidelity.check(sent, texts=sources):
                    self._drop("要点の一覧と意味がずれる概要の文")            # 数値の対象・条件・限定が、要点の一覧とずれている
                elif att is not None and not self._overview_supported(att, sent, sources):
                    self._drop("要点の一覧に無い内容の概要の文")              # 要点の一覧に無い内容
                else:
                    kept.append(sent)
        while len(kept) > 1 and len("".join(kept)) > OVERVIEW_MAX_CHARS:
            kept.pop()                                          # 長すぎるときは後ろの文から外す
        if not kept:
            return Section(sec.title, [])
        return Section(sec.title, [Claim(text="".join(kept), status=STATUS_OVERALL)])

    def _task_lines(self) -> list[str]:
        """概要に渡す「資料が読者に求めている作業」。課題カードの指示文と、作業を求める文（原文のまま、先頭の数件）。"""
        out: list[str] = []
        for card in self._tasks.cards:
            if card.labeled and card.do:
                head = card.title + (f"（{card.topic}）" if card.topic else "")
                out.append(f"{head}: {card.do[0].text}"[:140])
        out.extend(c.text[:140] for c in self._instr[: max(0, 4 - len(out))])
        return out

    def _source_of(self, ids: list[int]) -> str:
        """引用されたブロックと同じページにある全ブロックの本文（数値の実在確認用）。"""
        pages = {self._by_id[i].page for i in ids if i in self._by_id}
        return "\n".join(t for p in sorted(pages) for t in self._page_texts.get(p, []))

    def _pages_of(self, ids: set[int]) -> tuple[int, int] | None:
        pages = [self._by_id[i].page for i in ids if i in self._by_id]
        return (min(pages), max(pages)) if pages else None


# 結果・結論・貢献を述べる文（要点として残したい）
_FINDING = re.compile(r"結果|示唆|明らか|確認[さで]|分かる|わかる|読み取れ|判明|貢献|結論|提案|指摘|必要である|できる")
# 「〜こと」「〜点」で終わる名詞句（主語と述語が無く、何がどうなのかが伝わらない）
_DEFINITION = re.compile(r"(?:とは[、,，]|を意味する|を指す|を表す|と定義|と呼ばれ)")
_PAGE_FURNITURE = re.compile(r"(?:pp\.?\d|Vol\.?\d|Transactions|No\.\d)")
_HEAD_PARTICLE = re.compile(r"^(?:が|を|に|の|で|と|も|へ|や|は|から|まで|より|って)")


def _flat_text(s: str) -> str:
    return re.sub(r"\s+", "", s)


_FRAGMENT = re.compile(r"(?:こと|点|もの|ところ|状況|状態)[。.]?$")


def _pick_spread(items: list[Item], cap: int, scores: dict[int, float]) -> list[Item]:
    """重要度の高い要点を選ぶが、文書の一部（冒頭など）に偏らないよう、近いページから選ぶほど減点する。

    重要度だけで選ぶと、冒頭ページの加点で前半ばかりになり、後半の提案・結論が落ちる（実PDFで確認）。
    返す順序は元の順序（文書順）。
    """
    def pages(it: Item) -> tuple[int, int]:
        return it.pages if it.pages else (0, 0)

    base = {id(it): max((scores.get(i, 0.0) for i in it.ids), default=0.0) + (1.5 if _FINDING.search(it.text) else 0.0)
            for it in items}
    chosen: list[Item] = []
    remaining = list(items)
    while remaining and len(chosen) < cap:
        def key(it: Item) -> float:
            a, b = pages(it)
            near = sum(1 for c in chosen if pages(c)[0] <= b + 1 and a - 1 <= pages(c)[1])
            gap = min((max(0, max(a - pages(c)[1], pages(c)[0] - b)) for c in chosen), default=8)
            return base[id(it)] - 2.0 * near + 0.15 * min(gap, 8)       # 既に選んだ箇所から遠いほど優先
        best = max(remaining, key=lambda it: (key(it), -items.index(it)))
        remaining.remove(best)
        chosen.append(best)
    keep = {id(it) for it in chosen}
    return [it for it in items if id(it) in keep]


def _dedupe_items(items: list[Item]) -> list[Item]:
    """ほぼ同じ内容の部分要約を1つにする（文字2-gramの重なりが大きいもの）。"""
    from .attribution import _bigrams

    kept: list[Item] = []
    grams: list[set[str]] = []
    for it in items:
        g = _bigrams(it.text)
        if any(g and o and len(g & o) / min(len(g), len(o)) >= 0.7 for o in grams):
            continue
        kept.append(it)
        grams.append(g)
    return kept


def _items_tokens(items: list[Item]) -> int:
    return sum(estimate_tokens(i.text) + 14 for i in items)


_TEMPLATE_ECHO = re.compile(r"[」』）)\]]\s*する[。.]?$|[A-Za-z0-9_/.\-]\s+する[。.]?$")     # 書式例の「〜する」を機械的に付けた項目
_PROMPT_IN_TEXT = re.compile(r"\[[\w.\-]+@[\w.\-]+\]\s*[$#]|^\s*<\/?\w+")


def _is_code_echo(text: str) -> bool:
    """コマンドの実行例・タグ・書式の写しだけで、何をするのかが書かれていない項目。"""
    if _TEMPLATE_ECHO.search(text):
        return True
    if _PROMPT_IN_TEXT.search(text):
        ja = sum(1 for ch in text if "぀" <= ch <= "ヿ" or "一" <= ch <= "鿿")
        return ja < 0.25 * max(1, len(text.replace(" ", "")))
    return False


_DIRECTIVE_END = re.compile(r"(?:なさい|せよ|ください|ましょう)[。．.]?$")      # 資料の内容ではなく、読者への指示の形の文


def _sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[。．！？!?])", text) if p.strip()]


def _relabel(items: list[Item]) -> list[Item]:
    return [replace(it, label=f"S{n}") for n, it in enumerate(items, start=1)]


def _union_pages(group: list[Item], s: "Summarizer") -> tuple[int, int] | None:
    ids = {i for it in group for i in it.ids}
    return s._pages_of(ids)


def _page_span(pages: list[int]) -> str:
    from .schemas import format_pages
    return format_pages(pages)
