import pytest

from app.core.cancel import CancelToken
from app.core.config import Config
from app.core.errors import Cancelled, ContextOverflowError, SummaryError
from app.summary.schemas import STATUS_INFERRED, STATUS_OK, STATUS_RANGE, STATUS_UNRESOLVED
from app.summary.summarizer import Summarizer

from helpers import FakeEngine, blk, grounded_lines, ids_in, is_map, quotes

JP = "データベースの正規化では冗長性を減らして整合性を保つことが重要である。" * 12   # 約400文字


def sections_by_title(result):
    return {s.title: s for s in result.sections}


def small_cfg(**kw):
    base = dict(ctx_size=8192, fast_input_tokens=1200, chunk_tokens=700, single_pass_tokens=1000,
                reduce_input_tokens=3500, extractive_reduce=False)
    base.update(kw)
    return Config(**base).validate()


def final_answer(ref_a: str, ref_b: str = "") -> str:
    """JP（正規化の文）に基づく、検証を通る最終出力。"""
    return (
        "## 概要\n- データベースの正規化では冗長性を減らして整合性を保つことが重要である。\n\n"
        "## 主要ポイント\n- 正規化では冗長性を減らして整合性を保つ " + ref_a + "\n- 整合性を保つことが重要である " + ref_a + "\n\n"
        "## 詳細\n- 冗長性を減らして整合性を保つことが重要である " + (ref_b or ref_a) + "\n\n"
        "## 注意点・限界\n- 記載なし\n"
    )


# ------------------------------------------------------------ 高速モード

def test_fast_mode_validates_references_and_numbers():
    blocks = [blk(1, 1, "第1章", size=18, heading=True), blk(2, 1, "精度は95.2%である。"), blk(3, 2, JP[:120]),
              blk(4, 3, "再現率は88.8%であり、評価データは1000件である。")]

    def responder(system, user, n):
        assert "<文書>" in system and "命令" in system             # 資料中の命令に従わない旨を明示
        assert "[B1]" in user and "[B2]" in user
        return ("## 概要\n- 精度は95.2%で、再現率は88.8%であり、評価データは1000件である。\n## 主要ポイント\n- 精度は95.2%である [B2]\n- 精度は88.8%である [B2]\n"
                "- 精度は95.2%で、再現率は88.8%である [B2]\n"
                "- 精度は99.9%である [B2]\n- 存在しない根拠 [B999]\n- 原文に書かれていない宇宙の話 [B3]\n"
                "## 詳細\n- 正規化は冗長性を減らして整合性を保つ [B3]\n## 注意点・限界\n- 記載なし\n")

    eng = FakeEngine(responder)
    res = Summarizer(eng, small_cfg()).run(blocks, mode="fast", source_name="t.pdf")
    sec = sections_by_title(res)
    texts = [c.text for c in sec["主要ポイント"].claims]
    assert "精度は95.2%である" in texts and "精度は88.8%である" in texts                 # 別のページにある数値は、根拠を探し直して採用
    both = next(c for c in sec["主要ポイント"].claims if "再現率" in c.text)
    assert both.status in (STATUS_OK, STATUS_INFERRED)
    # 資料のどこにも無い数値・参照先が無く本文とも照合できない記述・原文に無い内容は、表示しない（推測で補わない）
    assert not any("99.9%" in t or "存在しない" in t or "宇宙" in t for t in texts)
    assert res.unverified_dropped == 3 and any("3件" in w and "表示していません" in w for w in res.warnings)
    assert len(eng.calls) == 1
    assert any("抜粋" in w for w in res.warnings)                  # 重要部分のみであることを明示
    text = res.render_text(show_sources=True)
    assert "参照箇所未特定" not in text and "99.9%" not in text and "[B2] p.1" in text


def test_fast_mode_retries_with_smaller_input_on_context_overflow():
    blocks = [blk(i, 1 + i // 10, JP[:200]) for i in range(1, 40)]
    sizes = []

    def responder(system, user, n):
        sizes.append(len(user))
        if n == 1:
            return ContextOverflowError("too long")
        return final_answer("[B1]")

    res = Summarizer(FakeEngine(responder), small_cfg(fast_input_tokens=1500)).run(blocks, mode="fast")
    assert len(sizes) == 2 and sizes[1] < sizes[0]
    assert res.sections


def test_unparsable_output_is_shown_with_warning_when_it_is_grounded_in_the_document():
    blocks = [blk(1, 1, JP[:100])]
    res = Summarizer(FakeEngine(lambda s, u, n: "データベースの正規化では冗長性を減らして整合性を保つことが重要である。"),
                     small_cfg()).run(blocks, mode="fast")
    assert res.sections[0].title == "要約"
    assert any("形式" in w for w in res.warnings)
    # 原文に無い内容だけのときは、そのまま見せずにエラーにする
    with pytest.raises(SummaryError):
        Summarizer(FakeEngine(lambda s, u, n: "要約です。宇宙ステーションの軌道高度について。"), small_cfg()).run(blocks, mode="fast")


def test_empty_input_and_empty_output_raise():
    with pytest.raises(SummaryError):
        Summarizer(FakeEngine(lambda s, u, n: ""), small_cfg()).run([], mode="fast")
    with pytest.raises(SummaryError):
        Summarizer(FakeEngine(lambda s, u, n: ""), small_cfg()).run([blk(1, 1, JP[:50])], mode="fast")


# ------------------------------------------------------------ 詳細モード

def test_detail_single_pass_for_short_documents():
    blocks = [blk(1, 1, JP[:150]), blk(2, 1, JP[:150])]
    eng = FakeEngine(lambda s, u, n: final_answer("[B1]"))
    res = Summarizer(eng, small_cfg()).run(blocks, mode="detail")
    assert len(eng.calls) == 1
    assert res.mode == "detail"


def _map_reduce_responder(system, user, n):
    if is_map(user):
        first = ids_in(user)[0]
        return grounded_lines(user, 1) + "- 範囲外を引用している [B9999]\n- 根拠なし\n"
    assert "[S1]" in user and "p." in user                          # reduce 入力には部分要約とページが載る
    return final_answer("[S1]", "[S2][S99]")


def test_detail_map_reduce_carries_provenance_from_program_side():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 13)]
    eng = FakeEngine(_map_reduce_responder)
    res = Summarizer(eng, small_cfg()).run(blocks, mode="detail", page_count=6)
    n_map = sum(is_map(u) for _, u, _ in eng.calls)
    assert n_map >= 4 and len(eng.calls) == n_map + 1                 # map × チャンク数 + reduce 1回
    assert sections_by_title(res)["概要"].claims[0].status == "overall"         # 概要は文書全体の要約（個別の参照は付けない）
    claim = sections_by_title(res)["主要ポイント"].claims[0]
    assert claim.status == STATUS_OK and claim.block_ids               # モデルは番号を返しただけ。IDへの解決はプログラム
    assert all(i in res.evidence for i in claim.block_ids)
    detail = sections_by_title(res)["詳細"].claims[0]
    assert "S99" not in detail.labels                                  # 存在しない部分要約番号は捨てられる
    assert res.pages_of(claim.block_ids)


def test_map_citations_outside_the_chunk_are_discarded_and_marked_range_only():
    # 1つの部分（チャンク）に、短い文が10個。要点はその全部にまたがる（1つのブロックでは説明できない）
    phrases = [f"{w}について{v}の確認を行った。" for w, v in zip(
        ["売上", "在庫", "物流", "採用", "広報", "調達", "品質", "法務", "経理", "開発"],
        ["月次", "週次", "四半期", "年次", "日次", "半期", "臨時", "定例", "個別", "全体"])]
    blocks = [blk(i + 1, 1, t) for i, t in enumerate(phrases)]
    spread = "".join(phrases)

    def responder(system, user, n):
        if is_map(user):
            return f"- {spread} [B9999]\n"                      # このチャンクのIDではない番号
        return "## 概要\n概要の文である。"

    cfg = small_cfg(single_pass_tokens=10, chunk_tokens=2000, extractive_reduce=True)
    res = Summarizer(FakeEngine(responder), cfg).run(blocks, mode="detail")
    claim = sections_by_title(res)["主要ポイント"].claims[0]
    assert claim.status == STATUS_RANGE and claim.block_ids          # チャンク全体（範囲）までしか特定できない
    assert "付近" in res.render_text()


def test_map_stage_splits_chunk_on_overflow():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 13)]
    state = {"failed": False}

    def responder(system, user, n):
        if is_map(user):
            if not state["failed"]:
                state["failed"] = True
                return ContextOverflowError("x")
            return grounded_lines(user, 1)
        return final_answer("[S1]")

    eng = FakeEngine(responder)
    res = Summarizer(eng, small_cfg(chunk_tokens=900)).run(blocks, mode="detail")
    assert res.sections and state["failed"]


def test_map_stage_splits_single_oversized_block_on_overflow():
    blocks = [blk(i, 1, JP * 2) for i in range(1, 6)]          # 1ブロック（約800文字）が1チャンクになる
    state = {"failed": False, "sizes": []}

    def responder(system, user, n):
        if is_map(user):
            state["sizes"].append(len(user))
            if not state["failed"]:
                state["failed"] = True
                return ContextOverflowError("x")
            return grounded_lines(user, 1)
        return final_answer("[S1]")

    res = Summarizer(FakeEngine(responder), small_cfg(chunk_tokens=900)).run(blocks, mode="detail")
    assert res.sections and state["failed"]
    assert state["sizes"][1] < state["sizes"][0]


def test_reduce_collapses_when_summaries_are_too_long():
    blocks = [blk(i, i, JP) for i in range(1, 21)]

    def responder(system, user, n):
        if is_map(user):
            q = quotes(user, width=400)
            return f"- {q[0][1]} [B{q[0][0]}]\n- {q[-1][1]} [B{q[-1][0]}]\n"          # 長い（元の文のままの）要点
        if "統合してください" in user:
            return "- 正規化では冗長性を減らして整合性を保つことが重要である [S1]\n"
        return final_answer("[S1]")

    eng = FakeEngine(responder)
    cfg = small_cfg(reduce_input_tokens=1500, chunk_tokens=700)
    res = Summarizer(eng, cfg).run(blocks, mode="detail")
    assert any("統合してください" in u for _, u, _ in eng.calls)
    assert res.sections


def test_time_budget_skips_low_priority_chunks_and_says_so():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 21)]
    clock = {"t": 0.0}

    def responder(system, user, n):
        clock["t"] += 100.0                                         # 1回の呼び出しに100秒かかる想定
        if is_map(user):
            return grounded_lines(user, 1)
        return final_answer("[S1]")

    eng = FakeEngine(responder)
    cfg = small_cfg(time_budget_s=450.0)
    res = Summarizer(eng, cfg, clock=lambda: clock["t"]).run(blocks, mode="detail")
    n_map = sum(is_map(u) for _, u, _ in eng.calls)
    assert 1 <= n_map < 8                                           # 全チャンクは処理していない
    assert any("時間予算" in w for w in res.warnings)


def test_cancel_stops_processing():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 13)]
    cancel = CancelToken()
    eng = FakeEngine(lambda s, u, n: grounded_lines(u, 1) if is_map(u) else final_answer("[S1]"),
                     on_call=lambda n: cancel.set() if n == 2 else None)
    with pytest.raises(Cancelled):
        Summarizer(eng, small_cfg(), cancel=cancel).run(blocks, mode="detail")
    assert len(eng.calls) == 2                                     # 中止後は追加の呼び出しをしない


def test_metrics_are_recorded():
    blocks = [blk(1, 1, JP[:100])]
    s = Summarizer(FakeEngine(lambda s_, u, n: final_answer("[B1]")), small_cfg())
    res = s.run(blocks, mode="fast")
    m = res.metrics
    assert len(m.calls) == 1 and m.completion_tokens == 50
    assert m.prompt_tps == 100.0 and m.gen_tps == 25.0
    assert any("入力処理速度" in line for line in m.report_lines())


def test_raw_hook_receives_model_output_only_when_given():
    blocks = [blk(1, 1, JP[:100])]
    seen = []
    Summarizer(FakeEngine(lambda s_, u, n: final_answer("[B1]")), small_cfg(),
               raw_hook=lambda label, text: seen.append((label, text))).run(blocks, mode="fast")
    assert seen and seen[0][0] == "fast" and "## 概要" in seen[0][1]
    Summarizer(FakeEngine(lambda s_, u, n: final_answer("[B1]")), small_cfg()).run(blocks, mode="fast")   # 指定なしでも動く


# ------------------------------------------------------------ 詳細モードの根拠（推定・途中切れ・簡体字）

def _distinct_blocks():
    texts = [
        "第三正規形まで進めると更新時の不整合を防げる。冗長なデータが約17.5%減少した。",
        "B木インデックスは範囲検索に強い。更新が多い表では書き込みが約11%遅くなる。",
        "分散システムではCAP定理により一貫性か可用性のどちらかを選ぶ必要がある。",
        "最小権限の原則でアカウントを管理し、暗号化しても鍵の管理が甘ければ意味がない。",
    ]
    return [blk(i + 1, i // 2 + 1, (t + "補足説明。") * 6) for i, t in enumerate(texts)]


def test_detail_claims_without_model_refs_are_attributed_by_the_program():
    blocks = _distinct_blocks()

    def responder(system, user, n):
        if is_map(user):
            return grounded_lines(user, 1)
        # reduce: 小型モデルが番号を一切付けない状況を再現
        return ("## 概要\n- 第三正規形、B木インデックス、分散システム、暗号化について述べている。\n\n## 主要ポイント\n"
                "- 第三正規形まで進めると更新時の不整合を防げる\n- 暗号化しても鍵の管理が甘ければ意味がない\n\n"
                "## 詳細\n- 無関係な宇宙ステーションの軌道高度は原文にない\n\n## 注意点・限界\n- 記載なし\n")

    res = Summarizer(FakeEngine(responder), small_cfg(single_pass_tokens=10, chunk_tokens=300)).run(blocks, mode="detail")
    sec = sections_by_title(res)
    inferred = sec["主要ポイント"].claims
    assert [c.status for c in inferred] == [STATUS_INFERRED, STATUS_INFERRED]
    assert inferred[0].block_ids == [1] and inferred[1].block_ids == [4]
    assert "詳細" not in sec                                          # 原文に無い内容は推定もされず、表示されない
    assert res.unverified_dropped == 1 and "宇宙" not in res.render_text()
    assert "本文との照合による推定" in res.render_text()


def test_truncated_last_map_item_is_dropped():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 13)]
    reduce_inputs = []

    def responder(system, user, n):
        if is_map(user):
            return (grounded_lines(user, 1) + "- 途中で切れた要点がここ", "length")
        reduce_inputs.append(user)
        return final_answer("[S1]")

    res = Summarizer(FakeEngine(responder), small_cfg()).run(blocks, mode="detail")
    assert reduce_inputs and "途中で切れた" not in reduce_inputs[0]
    assert "整合性を保つ" in reduce_inputs[0]
    assert any("打ち切られ" in w for w in res.warnings)


def test_simplified_chinese_characters_produce_a_warning():
    blocks = [blk(1, 1, JP[:100])]
    out = ("## 概要\n- データベースの正规化では冗長性を減らす [B1]\n## 主要ポイント\n- 正規化では冗長性を減らす [B1]\n"
           "## 詳細\n- 整合性を保つことが重要である [B1]\n## 注意点・限界\n- 記載なし\n")
    res = Summarizer(FakeEngine(lambda s_, u, n: out), small_cfg()).run(blocks, mode="fast")
    assert any("簡体字" in w and "规" in w for w in res.warnings)


def test_fast_mode_uses_lean_prompt_without_overlapping_sections():
    blocks = [blk(1, 1, JP[:100])]
    eng = FakeEngine(lambda s_, u, n: final_answer("[B1]"))
    Summarizer(eng, small_cfg()).run(blocks, mode="fast", kind="general")
    fast_prompt = eng.calls[0][1]
    assert "## 主要ポイント" in fast_prompt and "## 詳細" not in fast_prompt and "## 注意点" not in fast_prompt
    eng2 = FakeEngine(lambda s_, u, n: final_answer("[B1]"))
    Summarizer(eng2, small_cfg()).run(blocks, mode="fast", kind="paper")
    assert "## 詳細" in eng2.calls[0][1] and "研究目的" in eng2.calls[0][1] and "## 主要ポイント" not in eng2.calls[0][1]
    eng3 = FakeEngine(lambda s_, u, n: final_answer("[B1]"))
    Summarizer(eng3, small_cfg(single_pass_tokens=2000)).run(blocks, mode="detail", kind="general")
    assert "## 主要ポイント" in eng3.calls[0][1] and "## 詳細" in eng3.calls[0][1]      # 詳細モードは従来の全節


def test_truncated_last_line_of_final_output_is_dropped_and_reduce_has_no_overlapping_detail():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 13)]
    prompts_seen = []

    def responder(system, user, n):
        if is_map(user):
            return grounded_lines(user, 1)
        prompts_seen.append(user)
        return ("## 概要\n- 正規化は整合性を保つことが重要である。\n## 主要ポイント\n- 冗長性を減らして整合性を保つ [S1]\n"
                "- 正規化では冗長性を減らすことが重要である [S2]\n- [S1", "length")

    res = Summarizer(FakeEngine(responder), small_cfg()).run(blocks, mode="detail", kind="general")
    texts = [c.text for s in res.sections for c in s.claims]
    assert "正規化では冗長性を減らすことが重要である" in texts and not any("[S1" in t for t in texts)
    assert "## 詳細" not in prompts_seen[0] and "## 主要ポイント" in prompts_seen[0]    # 統合段階の一般資料は詳細節なし


# ------------------------------------------------------------ 一般資料の詳細モード: 部分要約をそのまま採用

def test_extractive_reduce_uses_map_items_verbatim_with_exact_pages_and_one_short_call():
    blocks = _distinct_blocks() * 1
    texts = {1: "第三正規形で更新時の不整合を防げる", 2: "B木インデックスは範囲検索に強い",
             3: "CAP定理で一貫性か可用性を選ぶ", 4: "暗号化しても鍵の管理が甘ければ意味がない"}

    def responder(system, user, n):
        if is_map(user):
            first = ids_in(user)[0]
            return f"- {texts[first]} [B{first}]\n- {texts[first]}です [B{first}]\n"        # 重複した要点も出す
        assert "要点の一覧" in user and "## 主要ポイント" not in user                       # 概要だけを短く生成
        return "## 概要\n資料は第三正規形、B木インデックス、CAP定理、暗号化について説明している。"

    cfg = small_cfg(single_pass_tokens=10, chunk_tokens=300, extractive_reduce=True)
    eng = FakeEngine(responder)
    res = Summarizer(eng, cfg).run(blocks, mode="detail", kind="general")
    sec = sections_by_title(res)
    assert sec["概要"].claims[0].text == "資料は第三正規形、B木インデックス、CAP定理、暗号化について説明している。"
    assert sec["概要"].claims[0].status == "overall"
    pts = sec["主要ポイント"].claims
    assert [c.text for c in pts] == [texts[1], texts[2], texts[3], texts[4]]      # 原文どおり・重複は除去・文書順
    assert all(c.status == STATUS_OK and len(c.block_ids) == 1 for c in pts)
    assert sum(1 for _, u, _ in eng.calls if not is_map(u)) == 1
    assert eng.calls[-1][2] == cfg.overview_max_output


def test_extractive_reduce_caps_items_and_keeps_paper_on_llm_reduce():
    words = ["正規化", "索引", "分散", "暗号化", "監視", "復旧", "権限", "統計", "遅延", "冗長", "照合", "整合"]

    def sentence(i: int) -> str:
        return f"{words[i % 12]}に関して{words[(i * 5) % 12]}の観点から{words[(i * 7) % 12]}を検討した結果{i}件が報告された。"

    blocks = [blk(i, (i + 1) // 2, sentence(i)) for i in range(1, 25)]
    cfg = small_cfg(single_pass_tokens=10, chunk_tokens=700, extractive_reduce=True, extractive_max_items=3)

    def responder(system, user, n):
        if is_map(user):
            return "".join(f"- {t} [B{i}]\n" for i, t in quotes(user, 4, width=200))
        return "## 概要\n正規化に関する検討の結果が報告されている。"

    res = Summarizer(FakeEngine(responder), cfg).run(blocks, mode="detail", kind="general")
    assert len(sections_by_title(res)["主要ポイント"].claims) == 3
    # 論文は構造（目的/方法/結果/限界）が必要なので、従来どおりモデルが統合する
    eng = FakeEngine(lambda s, u, n: (grounded_lines(u, 1, width=80) if is_map(u) else final_answer("[S1]")))
    jp_blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 25)]
    Summarizer(eng, cfg).run(jp_blocks, mode="detail", kind="paper")
    assert any("## 詳細" in u for _, u, _ in eng.calls)


def test_pick_spread_covers_the_whole_document_instead_of_only_the_start():
    from app.llm.prompts import Item
    from app.summary.summarizer import _pick_spread

    # 前半ほど重要度が高い（冒頭ページ加点）が、後半も選ばれること。1ページ1要点 × 20ページ
    items = [Item(f"S{p}", f"要点{p}", {p}, pages=(p, p)) for p in range(1, 21)]
    scores = {p: (3.0 if p <= 2 else 1.0) for p in range(1, 21)}
    picked = _pick_spread(items, 8, scores)
    pages = [it.pages[0] for it in picked]
    assert len(picked) == 8 and pages == sorted(pages)                 # 文書順
    assert max(pages) >= 17 and sum(1 for p in pages if p <= 10) <= 5  # 後半も入る
    assert 1 in pages                                                  # 重要な冒頭も残る


def test_fast_mode_hints_at_detail_mode_with_estimate_when_coverage_is_low():
    blocks = [blk(i, i, JP) for i in range(1, 41)]                      # 全文は予算よりずっと大きい
    res = Summarizer(FakeEngine(lambda s_, u, n: final_answer("[B1]")), small_cfg()).run(blocks, mode="fast")
    joined = "\n".join(res.warnings)
    assert "本文の約" in joined and "詳細モードなら全体を読めます" in joined and "分" in joined
    small = [blk(1, 1, JP[:60])]                                        # 全部読めた場合は勧めない
    res2 = Summarizer(FakeEngine(lambda s_, u, n: final_answer("[B1]")), small_cfg()).run(small, mode="fast")
    assert "詳細モードなら" not in "\n".join(res2.warnings)


def test_lean_prompt_tells_model_not_to_cite_in_overview():
    from app.llm import prompts
    from app.llm.prompts import Item
    text = prompts.final_prompt([Item("B1", "本文", {1})], kind="general", preface="前置き", example="B3", lean=True)
    assert "「概要」には番号を付けず" in text and "5〜8項目" in text and "200〜350字" in text


def test_overview_prompt_includes_document_title_and_map_prompt_forbids_vague_items():
    from app.llm import prompts
    ov = prompts.overview_prompt(["要点A"], "機械学習および演習 深層学習")
    assert "資料のタイトル: 機械学習および演習 深層学習" in ov
    assert "資料のタイトル" not in prompts.overview_prompt(["要点A"])
    assert "内容のない項目は書かず" in prompts.map_prompt([prompts.Item("B1", "本文", {1})], heading="", pages=(1, 1), max_points=3)


def test_vacuous_items_are_not_used_and_steps_section_is_omitted_when_none():
    blocks = _distinct_blocks()

    def responder(system, user, n):
        if is_map(user):
            i = ids_in(user)[0]
            return f"- 内容｜想定するネットワーク構成を説明する [B{i}]\n- 内容｜暗号化しても鍵の管理が甘ければ意味がない件{i} [B{i}]\n"
        return "概要の文である。"

    cfg = small_cfg(single_pass_tokens=10, chunk_tokens=300, extractive_reduce=True)
    res = Summarizer(FakeEngine(responder), cfg).run(blocks, mode="detail", kind="general")
    texts = [c.text for s_ in res.sections for c in s_.claims]
    assert not any("説明する" in t for t in texts)
    assert "やること・手順" not in sections_by_title(res)


# ------------------------------------------------------------ 手順の拾い方（作業を求める文・コマンド例の写し）

def _task_blocks():
    texts = [
        "第三正規形まで進めると更新時の不整合を防げる。冗長なデータが約17.5%減少した。",
        "【課題1】設定表を作成しなさい。ファイルは設定フォルダに保存し、完成したら提出すること。",
        "B木インデックスは範囲検索に強い。更新が多い表では書き込みが約11%遅くなる。",
        "※ 作成後、グループ内で確認し、副手の記載チェックを受けること。",
    ]
    return [blk(i + 1, i // 2 + 1, t) for i, t in enumerate(texts)]


# ------------------------------------------------------------ 課題カードがある資料（プログラムが抜き出す）

def _card_doc():
    texts = [
        "第三正規形まで進めると更新時の不整合を防げる。冗長なデータが約17.5%減少した。",
        "⚫【課題1】設定表を作成しなさい。",
        "※ 【課題1】の記載終了後、副手の記載チェックを受けること。",
        "B木インデックスは範囲検索に強い。更新が多い表では書き込みが約11%遅くなる。",
        "分散システムではCAP定理により一貫性か可用性のどちらかを選ぶ必要がある。",
    ]
    return [blk(i + 1, i // 2 + 1, t) for i, t in enumerate(texts)]


def test_detail_with_task_cards_uses_cards_for_steps_and_drops_model_steps_and_duplicates():
    blocks = _card_doc()
    maps = []

    def responder(system, user, n):
        if is_map(user):
            maps.append(user)
            i = ids_in(user)[0]
            return "- 課題1では設定表を作成する [B2]\n- B木は範囲検索に強い [B4]\n- 分散システムでは一貫性か可用性を選ぶ [B5]\n"
        return "資料は演習の手引きである。"

    cfg = small_cfg(single_pass_tokens=10, chunk_tokens=700, extractive_reduce=True)
    res = Summarizer(FakeEngine(responder), cfg).run(blocks, mode="detail", kind="general")
    assert [t.title for t in res.tasks] == ["課題1"]
    assert "手順｜" not in maps[0] and "★" not in maps[0]                         # モデルには内容だけを書かせる
    sec = sections_by_title(res)
    assert "やること・手順" not in sec
    assert not any("設定表を作成する" in c.text for c in sec["主要ポイント"].claims)       # カードと同じ箇所の要点は省く
    assert any("B木" in c.text for c in sec["主要ポイント"].claims)
    assert "## やること・手順（原文から抜き出し）" in res.render_text()
    assert {2, 3} <= set(res.evidence)                                            # カードの根拠ブロックも原文として保持


def test_fast_with_task_cards_drops_the_model_steps_section():
    blocks = _card_doc()
    seen = []

    def responder(system, user, n):
        seen.append(user)
        return ("## 概要\n- 資料は演習の手引きである。\n\n## 主要ポイント\n- B木は範囲検索に強い [B4]\n\n"
                "## やること・手順\n- 設定表を作成する [B2]\n")

    res = Summarizer(FakeEngine(responder), small_cfg(fast_input_tokens=1200)).run(blocks, mode="fast", kind="general")
    assert "やること・手順" not in seen[0] and "★" not in seen[0]
    assert "やること・手順" not in sections_by_title(res)                          # モデルが書いても、カードに任せる
    assert res.tasks and res.tasks[0].do[0].text == "設定表を作成しなさい。"


def test_missing_listed_task_is_reported_as_a_warning():
    blocks = _card_doc()
    blocks[0] = blk(1, 1, "⚫\n【課題1】設定表の作成\n⚫\n【課題2】総括")

    def responder(system, user, n):
        return "## 概要\n- 資料は演習の手引きである。\n\n## 主要ポイント\n- B木は範囲検索に強い [B3]\n"

    res = Summarizer(FakeEngine(responder), small_cfg(fast_input_tokens=1200)).run(blocks, mode="fast", kind="general")
    assert any("課題2" in w and "見つかりませんでした" in w for w in res.warnings)


