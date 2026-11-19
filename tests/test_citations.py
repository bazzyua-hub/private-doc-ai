from app.summary.citations import (
    check_numbers,
    clean_model_output,
    find_simplified,
    lines_to_items,
    parse_refs,
    resolve_sections,
    split_sections,
)
from app.summary.schemas import STATUS_INFERRED, STATUS_OK, STATUS_OVERALL, STATUS_RANGE, STATUS_UNRESOLVED, format_pages


# ------------------------------------------------------------ 出力の前処理

def test_clean_model_output_strips_think_and_fences():
    raw = "<think>\nうーん\n</think>\n```markdown\n## 概要\n- テスト\n```"
    assert clean_model_output(raw) == "## 概要\n- テスト"
    assert clean_model_output("考え中</think>本文") == "本文"


# ------------------------------------------------------------ 参照の取り出し

def test_parse_refs_variants():
    text, nums, wrong = parse_refs("結果は良好です。[B3][B7]", "B")
    assert text == "結果は良好です。" and nums == [3, 7] and wrong == 0
    assert parse_refs("要点 [B3, B5, 9]", "B")[1] == [3, 5, 9]
    assert parse_refs("要点 【B12】", "B")[1] == [12]
    assert parse_refs("要点 [B3-B5]", "B")[1] == [3, 4, 5]
    assert parse_refs("要点 (S2)", "S")[1] == [2]


def test_parse_refs_ignores_other_prefix_and_plain_brackets():
    text, nums, wrong = parse_refs("先行研究[1]では [S4] に基づく", "B")
    assert nums == [] and wrong == 1
    assert "[1]" in text                      # 論文の引用番号などは壊さない


def test_parse_refs_leaves_no_double_space_before_punctuation():
    assert parse_refs("結果です [B1]。", "B")[0] == "結果です。"


# ------------------------------------------------------------ セクション解析

MODEL_OUT = """## 概要
資料はデータベースの正規化を説明している。[B1]
続きの文。

## 主要ポイント
- 正規化で冗長性を減らす [B2]
- 第三正規形が実務で使われる [B3][B999]

**詳細**
・背景: 関係モデル [B1]
  折り返した行 [B4]

## 注意点・限界
- 特になし
"""


def test_split_sections_and_items():
    secs = split_sections(MODEL_OUT)
    assert [t for t, _ in secs] == ["概要", "主要ポイント", "詳細", "注意点・限界"]
    overview = lines_to_items(secs[0][1])
    assert len(overview) == 1 and "続きの文" in overview[0]       # 地の文は段落で1件
    detail = lines_to_items(secs[2][1])
    assert len(detail) == 1 and "折り返した行" in detail[0]       # インデントされた行は直前の項目の続き


def test_split_sections_alias_and_inline_content():
    secs = split_sections("### 主なポイント\n- a\n注意点：資料に記載なし")
    assert secs[0][0] == "主要ポイント"
    assert secs[1][0] == "注意点・限界" and secs[1][1] == ["資料に記載なし"]


def test_split_sections_returns_empty_when_no_known_headings():
    assert split_sections("ただの文章です。\n- 箇条書き") == []


# ------------------------------------------------------------ 数値の確認

def test_check_numbers():
    src = "精度は 95.2 ％ でした。サンプル数は1,200件、2026年に実施。"
    assert check_numbers("精度は95.2%。", src) == []
    assert check_numbers("サンプルは1200件", src) == []
    assert check_numbers("2026年に実施", src) == []
    assert check_numbers("精度は99.9%", src) == ["99.9%"]
    assert check_numbers("3つの手法", src) == []                       # 1桁の整数は対象外
    assert check_numbers("価格は150円", "価格は1500円") == ["150"]        # 部分一致で通さない
    assert check_numbers("１２３人", "123人") == []                      # 全角数字


# ------------------------------------------------------------ 検証つきの組み立て

def _resolve(text, *, prefix="B", allowed=None, coarse=None, block_text=None, block_page=None, doc="全文 12 34"):
    secs = split_sections(text)
    texts = block_text if block_text is not None else {1: "正規化の説明 95.2%", 2: "冗長性", 3: "第三正規形", 4: "折り返し"}
    pages = block_page or {i: i for i in texts}              # 既定は1ブロック=1ページ

    def source_of(ids):                                       # 引用ブロックと同じページの全ブロック
        wanted = {pages.get(i) for i in ids}
        return "\n".join(t for i, t in texts.items() if pages.get(i) in wanted)

    return resolve_sections(
        secs, prefix=prefix,
        allowed=allowed or {1: {1}, 2: {2}, 3: {3}, 4: {4}},
        coarse=coarse or set(),
        source_of=source_of,
        document_text=doc,
    )


def test_invalid_reference_becomes_unresolved_and_valid_is_kept():
    secs = _resolve(MODEL_OUT)
    claims = {c.text: c for s in secs for c in s.claims}
    ok = claims["正規化で冗長性を減らす"]
    assert ok.status == STATUS_OK and ok.block_ids == [2] and ok.labels == ["B2"]
    partial = claims["第三正規形が実務で使われる"]
    assert partial.block_ids == [3]                      # 存在しない B999 は捨てられ、B3 だけ残る
    none = _resolve("## 概要\n- 根拠の番号が存在しない [B999]\n")[0].claims[0]
    assert none.status == STATUS_UNRESOLVED and none.block_ids == []
    no_ref = _resolve("## 主要ポイント\n- 番号が付いていない主張\n")[0].claims[0]
    assert no_ref.status == STATUS_UNRESOLVED
    overview = _resolve("## 概要\n- 全体の要約で番号なし\n")[0].claims[0]
    assert overview.status == STATUS_OVERALL                   # 概要は参照なしを許容
    bad_overview = _resolve("## 概要\n- 存在しない番号 [B999]\n")[0].claims[0]
    assert bad_overview.status == STATUS_UNRESOLVED            # 番号を付けたのに存在しないなら未特定


def test_no_reference_needed_for_nothing_to_report():
    c = _resolve("## 注意点・限界\n- 特になし\n")[0].claims[0]
    assert c.status == STATUS_OK and c.unverified_numbers == []


def test_fabricated_number_is_flagged_against_cited_blocks():
    secs = _resolve("## 概要\n- 精度は95.2%でした [B1]\n- 精度は99.9%でした [B1]\n")
    good, bad = secs[0].claims
    assert good.unverified_numbers == []
    assert bad.unverified_numbers == ["99.9%"]


def test_number_is_checked_against_the_cited_page_not_the_whole_document():
    # 95.2% は B1（1ページ目）にある。別のページの B2 を根拠に挙げているので未確認になる
    c = _resolve("## 概要\n- 精度は95.2%でした [B2]\n")[0].claims[0]
    assert c.unverified_numbers == ["95.2%"]
    # 同じページの隣のブロックを引用した場合は確認できたものとする（実測で多かった誤検出への対策）
    same = _resolve("## 概要\n- 精度は95.2%でした [B2]\n", block_page={1: 1, 2: 1, 3: 2, 4: 2})[0].claims[0]
    assert same.unverified_numbers == []


def test_unresolved_claim_numbers_are_checked_against_whole_document():
    secs = _resolve("## 主要ポイント\n- 12人が参加\n- 77人が参加\n", doc="全文 12 34")
    a, b = secs[0].claims
    assert a.status == STATUS_UNRESOLVED and a.unverified_numbers == []
    assert b.unverified_numbers == ["77"]


def test_summary_references_resolve_to_union_of_blocks_and_range_only():
    secs = _resolve(
        "## 概要\n- 統合した要点 [S1][S2]\n- 粗い要点 [S3]\n",
        prefix="S",
        allowed={1: {10, 11}, 2: {12}, 3: {20, 21, 22}},
        coarse={3},
        block_text={},
    )
    merged, coarse = secs[0].claims
    assert merged.block_ids == [10, 11, 12] and merged.status == STATUS_OK
    assert coarse.status == STATUS_RANGE and coarse.block_ids == [20, 21, 22]


def test_format_pages():
    assert format_pages([3, 4, 5, 9]) == "p.3-5, 9"
    assert format_pages([2]) == "p.2"
    assert format_pages([]) == ""


# ------------------------------------------------------------ 新機能（部分要約の行解析・推定・簡体字）

def test_lines_to_items_per_line_mode_handles_unbulleted_and_mixed_lines():
    lines = ["[B3] 要点A  ", "・要点B [B4]", "- 要点C [B5]", "[ B182 ] 要点D", ""]
    assert lines_to_items(lines, join_paragraphs=False) == ["[B3] 要点A", "要点B [B4]", "要点C [B5]", "[ B182 ] 要点D"]
    assert lines_to_items(["最初の行", "続きの行"]) == ["最初の行続きの行"]      # 既定の挙動は変わらない


def test_parse_refs_accepts_spaces_inside_brackets():
    assert parse_refs("[ B182 ] セキュリティ", "B")[1] == [182]


def test_no_ref_ok_accepts_label_prefix():
    sec = [("注意点・限界", ["- 資料から読み取れない点：なし", "- 特になし"])]
    out = resolve_sections(sec, prefix="B", allowed={}, coarse=set(), source_of=lambda ids: "", document_text="")
    assert [c.status for c in out[0].claims] == [STATUS_OK, STATUS_OK]


def test_unresolved_claim_gets_inferred_source_when_attributor_finds_one():
    sec = [("主要ポイント", ["- 更新時の不整合を防げる", "- 出典の分からない文です"])]
    out = resolve_sections(
        sec, prefix="S", allowed={1: {7}}, coarse=set(),
        source_of=lambda ids: "更新時の不整合を防げる 冗長なデータが17.5%減少",
        document_text="",
        attribute=lambda t: [7, 8] if "不整合" in t else [],
    )
    a, b = out[0].claims
    assert a.status == STATUS_INFERRED and a.block_ids == [7, 8] and a.labels == []
    assert b.status == STATUS_UNRESOLVED


def test_inferred_claim_gets_number_check_against_inferred_pages():
    sec = [("詳細", ["- ロック待ちの上限は99.5秒に設定する"])]
    out = resolve_sections(
        sec, prefix="S", allowed={}, coarse=set(),
        source_of=lambda ids: "ロック待ちの上限は31.5秒に設定する",
        document_text="", attribute=lambda t: [3],
    )
    c = out[0].claims[0]
    assert c.status == STATUS_INFERRED and c.unverified_numbers == ["99.5"]


def test_model_citation_wins_over_attribution():
    sec = [("主要ポイント", ["- 要点です [S1]"])]
    out = resolve_sections(sec, prefix="S", allowed={1: {2}}, coarse=set(),
                           source_of=lambda ids: "", document_text="", attribute=lambda t: [9])
    c = out[0].claims[0]
    assert c.status == STATUS_OK and c.block_ids == [2]


def test_find_simplified_detects_only_simplified_forms():
    assert find_simplified("过度な正規化と复旧目標") == ["复", "过"]
    assert find_simplified("過度な正規化と復旧目標") == []


def test_instruction_echo_and_label_prefix_are_cleaned():
    sec = [("注意点・限界", ["- 資料に書かれていない前提条件や制約は「特になし」と書く。", "- 要点1：接近条件が影響する [B1]"])]
    out = resolve_sections(sec, prefix="B", allowed={1: {1}}, coarse=set(),
                           source_of=lambda ids: "", document_text="")
    assert [c.text for c in out[0].claims] == ["接近条件が影響する"]


def test_unbulleted_lines_in_list_sections_stay_separate_items():
    sec = [("主要ポイント", ["有効回答は864件だった [S1]", "認知割合は78.4%だった [S2]"])]
    out = resolve_sections(sec, prefix="S", allowed={1: {1}, 2: {2}}, coarse=set(),
                           source_of=lambda ids: "864件 78.4%", document_text="")
    assert [c.text for c in out[0].claims] == ["有効回答は864件だった", "認知割合は78.4%だった"]
    assert [c.block_ids for c in out[0].claims] == [[1], [2]]


def test_overview_still_joins_wrapped_lines():
    sec = [("概要", ["資料全体の要点を説明する", "続きの文である。"])]
    out = resolve_sections(sec, prefix="S", allowed={}, coarse=set(), source_of=lambda ids: "", document_text="")
    assert len(out[0].claims) == 1


def test_split_crammed_splits_space_separated_points_only_when_it_looks_crammed():
    from app.summary.citations import split_crammed
    t = "有効回答864件（72.0%回収率） 質問紙は属性や情報接触など7領域で構成 平均年齢52.8歳、18～64歳が614人 河川低地での認知割合は82.6%"
    parts = split_crammed(t)
    assert len(parts) == 4 and parts[0].startswith("有効回答864件")
    assert split_crammed("短い文 です") == ["短い文 です"]
    natural = "脳波（EEG） 振幅の変化を調べる実験課題を設定し、反応時間と回避選択を記録して分析する計画であり、結果の解釈には注意が必要である。"
    assert len(split_crammed(natural)) <= 2 or split_crammed(natural) == [natural]


def test_citation_with_unsupported_content_is_replaced_by_attribution_or_marked_unresolved():
    sec = [("主要ポイント", ["- 質問紙は七つの領域で構成した [S1]", "- 全く無関係な主張がここに書かれている [S1]"])]
    kw = dict(prefix="S", allowed={1: {7}}, coarse=set(), source_of=lambda ids: "", document_text="")
    out = resolve_sections(sec, support=lambda t, ids: 0.1, attribute=lambda t: [25] if "質問紙" in t else [], **kw)
    a, b = out[0].claims
    assert a.status == STATUS_INFERRED and a.block_ids == [25] and a.labels == []
    assert b.status == STATUS_UNRESOLVED and b.block_ids == []
    ok = resolve_sections(sec, support=lambda t, ids: 0.9, attribute=lambda t: [25], **kw)
    assert all(c.status == STATUS_OK and c.block_ids == [7] for c in ok[0].claims)     # 内容が合っていれば引用を尊重


def test_duplicate_claims_across_sections_are_dropped():
    sec = [("主要ポイント", ["- 質問紙は7領域で構成され、年齢が異なる。 [S1]"]),
           ("詳細", ["- 質問紙は7領域で構成され、年齢が異なる [S1]", "- 別の内容の文である。 [S1]"])]
    out = resolve_sections(sec, prefix="S", allowed={1: {1}}, coarse=set(), source_of=lambda ids: "", document_text="")
    assert len(out[0].claims) == 1 and [c.text for c in out[1].claims] == ["別の内容の文である。"]


def test_section_that_only_duplicated_another_is_omitted():
    sec = [("主要ポイント", ["- 質問紙は7領域で構成され、年齢が異なる [S1]"]), ("詳細", ["- 質問紙は7領域で構成され、年齢が異なる [S1]"])]
    out = resolve_sections(sec, prefix="S", allowed={1: {1}}, coarse=set(), source_of=lambda ids: "", document_text="")
    assert [s.title for s in out] == ["主要ポイント"]


def test_split_crammed_does_not_cut_between_a_number_and_its_unit():
    from app.summary.citations import split_crammed
    t = "ResNet が 2015 年に優勝し、100 層以上の学習を成功させ、深くした層でもエラーが大きくならないことを示した"
    assert split_crammed(t) == [t]


def test_is_vacuous_detects_content_free_items_only():
    from app.summary.citations import is_vacuous
    assert is_vacuous("想定するネットワーク構成を説明する") and is_vacuous("課題1〜4が記載されている。") is False  # 数字を含む文は残す
    assert is_vacuous("確認結果を示す") and is_vacuous("勾配降下法の種類を示す")
    assert not is_vacuous("畳み込み層はグリッド状データに有効である")
    assert not is_vacuous("ログを確認することを説明するために、tail -f /var/log/httpd/access_log を実行して末尾を表示する")
