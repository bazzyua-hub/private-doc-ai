"""要約の信頼性（原文にない情報を出さない・読者への指示だけを「やること」にする・確認できない記述は出さない）のテスト。

偽エンジンに「原文に基づく出力」と「原文に無い内容を混ぜた出力」を返させ、後者が表示されないことを確かめる。
"""
import re

from app.core.config import Config
from app.llm import prompts
from app.summary.schemas import Claim, Section, SummaryResult, pick_snippet
from app.summary.summarizer import OVERVIEW_MAX_CHARS, Summarizer
from app.summary.tasks import extract_instructions

from helpers import FakeEngine, blk, grounded_lines, is_map, quotes

JP = "データベースの正規化では冗長性を減らして整合性を保つことが重要である。" * 6


def small_cfg(**kw):
    base = dict(ctx_size=8192, fast_input_tokens=1200, chunk_tokens=700, single_pass_tokens=1000,
                reduce_input_tokens=3500, extractive_reduce=True)
    base.update(kw)
    return Config(**base).validate()


def by_title(res):
    return {s.title: s for s in res.sections}


# ------------------------------------------------------------ 読者への作業指示だけを「やること」にする

def test_only_sentences_that_ask_the_reader_to_act_are_picked_verbatim():
    blocks = [
        blk(1, 1, "進め方", heading=True),
        blk(2, 1, "本実験では試料を80度で加熱した。結果を表にまとめた。"),            # 過去形の記録
        blk(3, 1, "データを保存する前に、必ず確認してください。提出前に名前を記入すること。"),
        blk(4, 2, "データベースの正規化は冗長性を減らす手法である。"),                # 説明
        blk(5, 2, "次の問いに答えなさい。"),
        blk(6, 3, "到達目標", heading=True),
        blk(7, 3, "本講義の目的は、データベースの基礎を理解すること。"),             # 学習目標は指示ではない
        blk(8, 4, "[user@host]$ cd /tmp ください"),                                   # コード例
    ]
    got = extract_instructions(blocks)
    assert [c.text for c in got] == ["データを保存する前に、必ず確認してください。", "提出前に名前を記入すること。", "次の問いに答えなさい。"]
    assert [c.block_ids for c in got] == [[3], [3], [5]] and all(c.status == "ok" for c in got)


def test_instruction_sentences_already_in_task_cards_are_not_repeated_and_the_list_is_capped():
    blocks = [blk(i, 1, f"項目{i}を確認してください。") for i in range(1, 30)]
    assert len(extract_instructions(blocks)) == 10
    assert [c.block_ids for c in extract_instructions(blocks, exclude_ids={1, 2})][:1] == [[3]]
    dup = [blk(1, 1, "名前を記入すること。"), blk(2, 2, "名前を記入すること。")]
    assert len(extract_instructions(dup)) == 1


def test_no_instruction_means_the_section_says_not_applicable_instead_of_inventing_tasks():
    blocks = [blk(1, 1, JP[:200]), blk(2, 2, JP[:200])]

    def responder(system, user, n):
        return ("## 概要\n- データベースの正規化では冗長性を減らして整合性を保つことが重要である。\n\n## 主要ポイント\n"
                "- 正規化は整合性を保つことが重要である [B1]\n"
                "## やること・手順\n- バックアップを取得する [B1]\n- 管理者に報告する [B2]\n")       # モデルが作った作業指示

    res = Summarizer(FakeEngine(responder), small_cfg()).run(blocks, mode="fast")
    assert "やること・手順" not in by_title(res)                         # モデルの「やること」は使わない
    assert res.tasks == [] and res.instructions == []
    text = res.render_text()
    assert "## やること・手順" in text and "該当なし（資料内に、読者への明示的な作業指示は見つかりませんでした）" in text
    assert "バックアップ" not in text and "管理者に報告" not in text


def test_explicit_instructions_in_the_document_are_shown_verbatim_with_pages():
    blocks = [blk(1, 1, JP[:200]), blk(2, 2, "提出前に必ず名前を記入すること。"), blk(3, 3, "不明点は担当者に確認してください。")]
    res = Summarizer(FakeEngine(lambda s, u, n: "## 概要\n- 正規化は整合性を保つことが重要である。\n## 主要ポイント\n- 冗長性を減らして整合性を保つ [B1]\n"),
                     small_cfg()).run(blocks, mode="fast")
    assert [c.text for c in res.instructions] == ["提出前に必ず名前を記入すること。", "不明点は担当者に確認してください。"]
    text = res.render_text()
    assert "### 作業を求める文" in text and "- 提出前に必ず名前を記入すること。（p.2）" in text and "該当なし" not in text


def test_model_is_never_asked_to_write_steps_in_any_prompt():
    seen = []

    def responder(system, user, n):
        seen.append(user)
        return (grounded_lines(user, 1) if is_map(user) else
                "## 概要\n- 正規化は整合性を保つことが重要である。\n## 主要ポイント\n- 冗長性を減らして整合性を保つ [B1]\n")

    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 9)]
    Summarizer(FakeEngine(responder), small_cfg()).run(blocks, mode="fast")
    Summarizer(FakeEngine(responder), small_cfg(ctx_size=32768, single_pass_tokens=20000)).run(blocks[:2], mode="detail")
    Summarizer(FakeEngine(responder), small_cfg(single_pass_tokens=10, chunk_tokens=300)).run(blocks, mode="detail")
    assert len(seen) >= 4
    assert not any("やること・手順" in u or "★" in u or "手順｜" in u for u in seen)
    assert "作業指示を新しく作らないで" in prompts.SYSTEM and "確認できないことは「記載なし」" in prompts.SYSTEM


def test_main_points_written_as_commands_are_dropped_because_instructions_come_from_the_document():
    blocks = [blk(1, 1, JP[:200]), blk(2, 2, JP[:200])]
    out = ("## 概要\n- 正規化は整合性を保つことが重要である。\n## 主要ポイント\n"
           "- 冗長性を減らして整合性を保ってください [B1]\n- 冗長性を減らして整合性を保つ [B1]\n")
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    assert [c.text for c in by_title(res)["主要ポイント"].claims] == ["冗長性を減らして整合性を保つ"]
    assert res.unverified_dropped == 1


# ------------------------------------------------------------ 原文で確認できない記述は表示しない

# 実モデルの出力（青葉市の架空の調査報告書）で、原文に無い枠組みを足した要点（照合の得点 0.40 / 0.44）
B105 = "完全回答者612人を解析対象とした。推定値は関連の強さを示し、因果効果を意味しない。小さな群の推定は不安定になりうるため、p値だけでなく効果量と不確実性を併記する。"
B81 = "年齢だけで支援対象を決めず、本人が希望する支援、移動手段、連絡方法を確認する必要がある。本人の同意なく支援情報を共有すれば、プライバシーと信頼を損なう。"


def test_real_model_points_that_add_content_not_in_the_source_are_dropped_but_faithful_ones_kept():
    blocks = [blk(105, 1, B105), blk(81, 2, B81),
              blk(7, 3, "青葉市を対象にした質問紙調査を実施し、有効回答864件（回収率72.0%）を得た。")]
    out = ("## 概要\n- 青葉市を対象にした質問紙調査の実施と、解析対象や支援情報の扱いについて述べている。\n## 主要ポイント\n"
           "- 再現性の評価には推定値とp値、効果量と不確実性の併記が求められている [B105]\n"
           "- 支援対象の判断には本人の同意が必要であり、情報共有はプライバシーと信頼の観点から慎重に扱う必要がある [B81]\n"
           "- 青葉市を対象にした質問紙調査を実施し、有効回答864件（回収率72.0%）を得た [B7]\n"
           "- 完全回答者612人を解析対象とした [B105]\n")
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    texts = [c.text for c in by_title(res)["主要ポイント"].claims]
    assert texts == ["青葉市を対象にした質問紙調査を実施し、有効回答864件（回収率72.0%）を得た", "完全回答者612人を解析対象とした"]
    assert res.unverified_dropped == 2


def test_numbers_that_appear_nowhere_in_the_document_drop_the_claim_even_in_the_overview():
    blocks = [blk(1, 1, "売上は1200万円で、前年から15%増加した。原因は新製品の発売である。")]
    out = ("## 概要\n- 売上は1200万円で、前年から15%増加した。利益率は42%に達した。\n## 主要ポイント\n"
           "- 売上は1200万円で、前年から15%増加した [B1]\n- 売上は2500万円に達した [B1]\n")
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    assert by_title(res)["概要"].claims[0].text == "売上は1200万円で、前年から15%増加した。"
    assert [c.text for c in by_title(res)["主要ポイント"].claims] == ["売上は1200万円で、前年から15%増加した"]
    assert res.unverified_dropped == 2


def test_overview_sentences_not_supported_by_the_points_are_dropped():
    blocks = [blk(1, 1, "第三正規形まで進めると更新時の不整合を防げる。冗長なデータが約17.5%減少した。"),
              blk(2, 2, "B木インデックスは範囲検索に強い。更新が多い表では書き込みが約11%遅くなる。")]
    out = ("## 概要\n- 第三正規形とB木インデックスについて述べている。今後は全国の自治体へAIの自動通知システムを導入すべきである。\n"
           "## 主要ポイント\n- 第三正規形まで進めると更新時の不整合を防げる [B1]\n- B木インデックスは範囲検索に強い [B2]\n")
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    ov = by_title(res)["概要"].claims[0]
    assert ov.text == "第三正規形とB木インデックスについて述べている。" and ov.status == "overall"
    assert "自治体" not in res.render_text() and res.unverified_dropped == 1


def test_when_the_whole_overview_is_unsupported_it_is_rebuilt_from_confirmed_points():
    blocks = [blk(1, 1, "第三正規形まで進めると更新時の不整合を防げる。"), blk(2, 2, "B木インデックスは範囲検索に強い。")]
    out = ("## 概要\n- 宇宙ステーションの軌道高度に関する議論である。\n## 主要ポイント\n"
           "- 第三正規形まで進めると更新時の不整合を防げる [B1]\n- B木インデックスは範囲検索に強い [B2]\n")
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    ov = by_title(res)["概要"].claims[0].text
    assert "宇宙" not in ov and "第三正規形まで進めると更新時の不整合を防げる。" in ov
    assert any("主要ポイントから構成" in w for w in res.warnings)


def test_overview_is_trimmed_from_the_back_when_too_long_and_never_padded_when_short():
    sents = [f"項目{i}については、データベースの正規化で冗長性を減らして整合性を保つことが重要であると述べている。" for i in range(1, 9)]
    blocks = [blk(1, 1, JP)]
    long_out = "## 概要\n- " + "".join(sents) + "\n## 主要ポイント\n- 正規化は整合性を保つことが重要である [B1]\n"
    res = Summarizer(FakeEngine(lambda s, u, n: long_out), small_cfg()).run(blocks, mode="fast")
    ov = by_title(res)["概要"].claims[0].text
    assert 200 <= len(ov) <= OVERVIEW_MAX_CHARS and ov.endswith("述べている。")
    short_out = "## 概要\n- 正規化の資料である。\n## 主要ポイント\n- 正規化は整合性を保つことが重要である [B1]\n"
    res2 = Summarizer(FakeEngine(lambda s, u, n: short_out), small_cfg()).run(blocks, mode="fast")
    assert by_title(res2)["概要"].claims[0].text == "正規化の資料である。"          # 内容が少なければ短いまま


def test_map_items_not_found_in_the_source_are_dropped_before_selection():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 9)]

    def responder(system, user, n):
        if is_map(user):
            return grounded_lines(user, 1, width=40) + "- 宇宙ステーションでは重力が弱いため筋肉が衰える [B1]\n"
        return "## 概要\n正規化は整合性を保つことが重要である。"

    res = Summarizer(FakeEngine(responder), small_cfg(single_pass_tokens=10, chunk_tokens=300)).run(blocks, mode="detail")
    texts = [c.text for c in by_title(res)["主要ポイント"].claims]
    assert texts and not any("宇宙" in t for t in texts)
    assert res.unverified_dropped >= 1 and any("表示していません" in w for w in res.warnings)


def test_main_points_are_capped_at_eight_and_ten_for_long_documents():
    def sentence(i):
        words = ["売上", "在庫", "物流", "採用", "広報", "調達", "品質", "法務", "経理", "開発", "営業", "保守"]
        return f"{words[i % 12]}部門の{words[(i * 5 + 1) % 12]}課題として{words[(i * 7 + 2) % 12]}の件数が{i * 3 + 11}件に達した。"

    def run(pages):
        blocks = [blk(i, i * pages // 40 + 1, sentence(i)) for i in range(1, 41)]

        def responder(system, user, n):
            if is_map(user):
                return "".join(f"- {t} [B{i}]\n" for i, t in quotes(user, 4, width=200))
            return "## 概要\n売上部門の課題が述べられている。"

        return Summarizer(FakeEngine(responder), small_cfg(chunk_tokens=300)).run(blocks, mode="detail")

    assert len(by_title(run(10))["主要ポイント"].claims) == 8
    assert len(by_title(run(40))["主要ポイント"].claims) == 10


# ------------------------------------------------------------ 根拠の原文は、抽出したテキストから取る

def test_evidence_snippet_is_a_part_of_the_extracted_text_chosen_for_the_claim():
    text = ("本研究では、住民の情報認知率を調べた。" + "背景の説明が続く。" * 12 + "認知率は78.4%であり、自宅外移動率は31.6%だった。" + "補足の説明が続く。" * 12)
    res = SummaryResult(mode="fast", doc_kind="general", source_name="x", page_count=1,
                        sections=[Section("主要ポイント", [Claim("認知率は78.4%、自宅外移動率は31.6%だった", ["B1"], [1], "ok")])],
                        evidence={1: blk(1, 1, text)})
    (bid, page, snip), = res.evidence_snippets(80)
    assert bid == 1 and page == 1 and "78.4%" in snip and "31.6%" in snip and len(snip) <= 82
    plain = snip.strip("…").replace(" ", "")
    assert plain in text                                           # 抽出済みの本文の一部そのもの（書き換えていない）
    assert pick_snippet("短い本文。", ["何か"], 80) == "短い本文。"


def test_every_displayed_source_is_a_substring_of_the_extracted_blocks():
    blocks = [blk(1, 1, JP[:300]), blk(2, 2, "提出前に必ず名前を記入すること。")]
    out = "## 概要\n- 正規化は整合性を保つことが重要である。\n## 主要ポイント\n- 冗長性を減らして整合性を保つ [B1]\n"
    res = Summarizer(FakeEngine(lambda s, u, n: out), small_cfg()).run(blocks, mode="fast")
    texts = {b.id: b.text for b in blocks}
    shown = res.evidence_snippets(60)
    assert shown
    for bid, _page, snip in shown:
        assert snip.strip("…").replace("…", "") in texts[bid]
    assert "[B2] p.2: 提出前に必ず名前を記入すること。" in res.render_text(show_sources=True)
