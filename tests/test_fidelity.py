"""要約が原文の意味を保っているか（数値が指す対象・因果・条件・限定・否定・方向・概念の区別）の検査のテスト。

数値や単語が原文にあるだけでは足りない。「大多数の態度が0.7〜0.8に収束」を「態度と本心の分布が0.7〜0.8で収束」と
書き換えるような、意味のずれを検出して、原文の文に置き換える（できなければ使わない）ことを確かめる。
"""
import re

from app.core.config import Config
from app.summary.attribution import Attributor
from app.summary.fidelity import FidelityChecker, split_sentences
from app.summary.summarizer import Summarizer

from helpers import FakeEngine, blk, ids_in, is_map

TEXTS = [
    "意見ダイナミクスのモデルでは、各エージェントは表明される態度と、内心にある本心の2つの値を持つ。両者は区別して扱う。",
    "シミュレーションの結果、大多数の態度が0.7〜0.8の間に収束した。一方、本心は初期値のまま大きく変化しなかった。",
    "同調圧力が強い場合に限り、態度と本心の乖離が拡大する可能性がある。",
    "ネットワークの規模は1000ノードとし、平均次数は6とした。",
    "分布の広がりは初期条件に依存するため、結論は一般化できない。",
]


def _blocks():
    return [blk(i + 1, 1 + i // 2, t) for i, t in enumerate(TEXTS)]


def _checker():
    bl = _blocks()
    att = Attributor(bl)
    return FidelityChecker(bl, att), att


def _kinds(claim, ids):
    fc, _ = _checker()
    return [i.kind for i in fc.check(claim, ids)]


# ------------------------------------------------------------ 数値が指す対象

def test_number_keeps_its_subject__reported_case():
    """実際に起きた例: 数値は正しいが、対象に原文の別の概念（本心・分布）が混ざった。"""
    kinds = _kinds("態度と本心の分布が0.7〜0.8の範囲で収束した。", [2])
    assert "数値の対象がずれている" in kinds


def test_faithful_paraphrase_and_verbatim_are_not_flagged():
    assert _kinds("大多数の態度は0.7〜0.8の間に収束した。", [2]) == []
    assert _kinds("大多数の態度が0.7〜0.8の間に収束した。", [2]) == []
    assert _kinds("ネットワークは1000ノードで、平均次数は6。", [4]) == []


def test_number_attached_to_another_thing_is_flagged():
    assert "数値の対象がずれている" in _kinds("ネットワークの規模は0.7〜0.8ノードとした。", [2, 4])


def test_number_scope_stops_at_sentence_end():
    """別の文にある数値の対象を、同じ節として扱わない（句点で区切る）。"""
    claim = "第三正規形で更新時の不整合を防げる。冗長なデータが約17.5%減少した。"
    bl = [blk(1, 1, "第三正規形で更新時の不整合を防げる。冗長なデータが約17.5%減少した。")]
    fc = FidelityChecker(bl, Attributor(bl))
    assert fc.check(claim, [1]) == []


def test_derived_word_paraphrase_is_tolerated():
    """「理解率」と「理解した割合」のような派生語の言い換えは、ずれとしない。"""
    bl = [blk(1, 1, "三問すべてを正しく理解した割合は42.2%だった。"), blk(2, 2, "理解率の差は後で検討する。"),
          blk(3, 3, "別の話。" * 5), blk(4, 3, "別の話題。" * 5), blk(5, 3, "さらに別。" * 5)]
    fc = FidelityChecker(bl, Attributor(bl))
    assert fc.check("三問の理解率は42.2%だった。", [1]) == []


# ------------------------------------------------------------ 因果・条件・限定・否定・方向・全称

def test_invented_causal_link_is_flagged():
    assert "原文に無い因果関係" in _kinds("大多数の態度が0.7〜0.8の間に収束したため、同調圧力が弱まった。", [2])


def test_purpose_noun_from_a_heading_is_not_flagged():
    """「誤解防止」という見出しの下の文を「誤解防止のため、…」と書くのは、原文にある語を使った言い換え。"""
    bl = [blk(1, 1, "誤解防止", heading=True), blk(2, 1, "地図上の範囲と更新時刻を明記し、最新状況を確認できるようにする。")]
    fc = FidelityChecker(bl, Attributor(bl))
    assert fc.check("誤解防止のため、地図上の範囲と更新時刻を明記し、最新状況を確認できるようにする。", [2]) == []
    # 動詞＋ため（目的・原因を新しく書いた形）は、原文に無ければ指摘する
    assert "原文に無い因果関係" in [i.kind for i in fc.check("最新状況を確認するため、地図上の範囲と更新時刻を明記する。", [2])]


def test_hedge_is_not_turned_into_assertion():
    assert "断定に変わっている" in _kinds("同調圧力が強い場合に限り、態度と本心の乖離が拡大する。", [3])
    assert _kinds("同調圧力が強い場合に限り、態度と本心の乖離が拡大する可能性がある。", [3]) == []


def test_dropped_negation_is_flagged():
    assert "否定が落ちている" in _kinds("分布の広がりは初期条件に依存し、結論は一般化できる。", [5])
    assert "否定が落ちている" in _kinds("本心は初期値から大きく変化した。", [2])


def test_most_does_not_become_all():
    assert "「一部・多く」が「すべて」に変わっている" in _kinds("すべての態度が0.7〜0.8の間に収束した。", [2])


def test_direction_reversal_is_flagged():
    bl = [blk(1, 1, "正理解群では移動率が上昇した。")]
    fc = FidelityChecker(bl, Attributor(bl))
    assert "方向が逆" in [i.kind for i in fc.check("正理解群では移動率が低下した。", [1])]


def test_distinct_concepts_are_not_merged():
    """原文のどの文にも並んで出てこない2つの語を「AとBの…」とひとまとめにしない。"""
    bl = [blk(1, 1, "態度は表明される意見である。"), blk(2, 1, "本心は内心にある意見である。"),
          blk(3, 2, "規模は1000ノードとした。" * 3), blk(4, 2, "次数は6とした。" * 3), blk(5, 3, "別の話題を扱う。" * 4),
          blk(6, 3, "さらに別の話題を扱う。" * 4)]
    fc = FidelityChecker(bl, Attributor(bl))
    assert "別々の概念をひとまとめにしている" in [i.kind for i in fc.check("態度と本心の分布は一致する。", [1, 2])]
    assert fc.check("態度は表明される意見である。", [1]) == []


def test_concept_from_another_page_is_flagged():
    bl = [blk(1, 1, "大多数の態度が収束した。" * 3), blk(2, 2, "本心は変化しなかった。" * 3), blk(3, 3, "別の話題。" * 5),
          blk(4, 3, "別の話題です。" * 5), blk(5, 3, "さらに別の話題。" * 5), blk(6, 4, "最後の話題。" * 5)]
    fc = FidelityChecker(bl, Attributor(bl))
    assert "根拠のページに無い、他の箇所の語" in [i.kind for i in fc.check("大多数の態度と本心が収束した。", [1])]


# ------------------------------------------------------------ 置き換え

def test_restore_returns_text_taken_verbatim_from_the_source():
    fc, _ = _checker()
    fixed = fc.restore("態度と本心の分布が0.7〜0.8の範囲で収束した。", [2])
    assert fixed and "0.7〜0.8" in fixed and "本心の分布" not in fixed
    flat = "".join(split_sentences(TEXTS[1]))
    assert fixed.rstrip("。") in flat                                       # 原文の部分文字列そのもの


def test_restore_gives_up_when_nothing_close_exists():
    fc, _ = _checker()
    assert fc.restore("ネットワークの規模は0.7〜0.8ノードとした。", [2]) is None


def test_restore_keeps_numbers_of_two_sentences():
    bl = [blk(1, 1, "指定避難所へ移動した人は142人、親族宅へ移動した人は72人だった。自宅の上階へ移動した人は96人だった。")]
    fc = FidelityChecker(bl, Attributor(bl))
    fixed = fc.restore("指定避難所へ移動した人数が142人、親族宅へ72人、自宅の上階へ96人だった。", [1])
    assert fixed and all(n in fixed for n in ("142", "72", "96"))


def test_every_real_sentence_passes_as_its_own_summary():
    """原文の文をそのまま要約にしたときは、1件も指摘しない（誤検出が無いこと）。"""
    fc, att = _checker()
    for b in _blocks():
        for s in split_sentences(b.text):
            assert fc.check(s, [b.id]) == [], s


# ------------------------------------------------------------ 要約の流れの中での動作

def _doc_blocks():
    return [blk(i + 1, 1 + i // 2, t * 3) for i, t in enumerate(TEXTS)]


def _run(model_point: str, overview: str = "意見の収束を調べたシミュレーションの資料である。"):
    blocks = _doc_blocks()

    def responder(system, user, n):
        if is_map(user):
            ids = ids_in(user)
            return f"- {model_point} [B{ids[0] if len(ids) < 2 else ids[1]}]\n"
        return overview

    cfg = Config(ctx_size=8192, fast_input_tokens=1200, chunk_tokens=700, single_pass_tokens=10,
                 reduce_input_tokens=3500, extractive_reduce=True).validate()
    return Summarizer(FakeEngine(responder), cfg).run(blocks, mode="detail", kind="general")


def test_pipeline_replaces_a_shifted_claim_with_the_source_sentence():
    res = _run("態度と本心の分布が0.7〜0.8の範囲で収束した。")
    points = [c.text for s in res.sections if s.title == "主要ポイント" for c in s.claims]
    assert points and not any("本心の分布" in p for p in points)
    assert any("大多数の態度が0.7〜0.8の間に収束した" in p for p in points)
    assert res.fidelity_replaced >= 1
    assert any("原文の文にそのまま置き換えました" in w for w in res.warnings)
    assert res.to_dict()["fidelity_replaced"] == res.fidelity_replaced


def test_pipeline_keeps_a_faithful_claim_unchanged():
    res = _run("大多数の態度は0.7〜0.8の間に収束した。")
    points = [c.text for s in res.sections if s.title == "主要ポイント" for c in s.claims]
    assert points == ["大多数の態度は0.7〜0.8の間に収束した。"]
    assert res.fidelity_replaced == 0


def test_pipeline_drops_an_overview_sentence_whose_number_target_shifted():
    res = _run("大多数の態度は0.7〜0.8の間に収束した。",
               overview="態度と本心の分布が0.7〜0.8の範囲で収束した。")
    ov = [c.text for s in res.sections if s.title == "概要" for c in s.claims]
    assert not any("本心の分布" in t for t in ov)
    assert res.unverified_dropped >= 1


# ------------------------------------------------------------ 実PDF（態度と本心）で見つかった問題

SRC_P4 = ("見ると、大多数の態度が0.7 から0.8 の間に収束していることが分かるので、ほぼ単一の規範が存在するものの"
          "その規範を支持しているグループとそうでないグループが生じており、部分的に多元的無知が維持されていることが分かる。")


def _paper_blocks():
    """「態度」「本心」が資料の中心概念（多くのブロックに出る）である短い資料。"""
    texts = [
        "各エージェントは態度と本心を持つ。態度は他者に示す値で、本心は内心の値である。",
        "態度の初期値は0.5から1.0、本心の初期値は0から0.5とする。",
        "態度と本心の差の分布を見ると、制裁の大小で多元的無知の発生度合いが異なる。",
        SRC_P4,
        "本研究の貢献の1つは規範の性質の違いが多元的無知の解消に与える影響を指摘した点である。",
        "態度と本心が一致するグループと一致しないグループに二極化する。",
        "本心を表明する個人の存在が解消につながると想定された。",
    ]
    return [blk(i + 1, 1 + i // 2, t) for i, t in enumerate(texts)]


def test_core_concepts_of_a_short_document_are_checked_too():
    """「本心」「態度」が多くのブロックに出る資料でも、数値の対象のすり替えを見逃さない（実PDFで見逃した）。"""
    bl = _paper_blocks()
    fc = FidelityChecker(bl, Attributor(bl))
    assert "数値の対象がずれている" in [i.kind for i in fc.check("態度と本心の分布が0.7〜0.8の範囲で収束している。", [4])]
    assert fc.check("大多数の態度は0.7〜0.8の間に収束している。", [4]) == []


def test_restore_tidies_pdf_spaces_and_drops_the_lead_fragment():
    bl = _paper_blocks()
    fc = FidelityChecker(bl, Attributor(bl))
    fixed = fc.restore("態度と本心の分布が0.7〜0.8の範囲で収束している。", [4])
    assert fixed.startswith("大多数の態度が0.7から0.8の間に収束している")      # 「見ると、」を外し、数字まわりの空白を詰める
    assert "0.7 から" not in fixed


def test_overview_connectives_are_a_paraphrase_not_an_invented_cause():
    """概要は要点を言い換えてまとめる文。「結果として」「ため」は言い換えで現れるので、足された因果とはしない。"""
    bl = _paper_blocks()
    fc = FidelityChecker(bl, Attributor(bl))
    points = ["態度と本心が一致するグループと一致しないグループに二極化する", "大多数の態度が0.7から0.8の間に収束している"]
    assert fc.check("結果として、態度と本心が一致するグループと一致しないグループに二極化する。", texts=points) == []
    # ただし、原因を言い切る語や、数値の対象のすり替えは、概要でも指摘する
    assert fc.check("態度と本心の分布が0.7〜0.8の範囲で収束している。", texts=points)


def _paper_run(points, overview):
    bl = [blk(i + 1, 1 + i // 2, t) for i, t in enumerate([
        "多元的無知とは、多くの成員が本心では規範を受け入れていないのに、他の成員は受け入れていると信じている状況を意味する。" * 2,
        "本研究ではコンピュータ・シミュレーションを活用して、多元的無知の解消を検討する。" * 2,
        "Huang and Wen（2014）のモデルに制裁の強さと本心の表明行動を加えたモデルを分析に用いる。" * 2,
        "エージェントは毎期、態度と本心の更新を行い、100期繰り返す。" * 2,
        SRC_P4,
        "本研究の貢献の1つは規範の性質の違いが多元的無知の解消に与える影響を指摘した点である。" * 2,
        "制裁が強い規範では多元的無知がより根強く維持される可能性が示唆された。" * 2,
    ])]

    def responder(system, user, n):
        if is_map(user):
            ids = set(ids_in(user))
            out = [f"- {p} [B{i}]" for p, i in points if i in ids]
            return "\n".join(out) or "- 要点なし"
        return overview

    cfg = Config(ctx_size=8192, fast_input_tokens=1200, chunk_tokens=120, single_pass_tokens=10,
                 reduce_input_tokens=3500, extractive_reduce=True).validate()
    return Summarizer(FakeEngine(responder), cfg).run(bl, mode="detail", kind="general")


def test_noun_phrase_fragments_are_widened_to_the_source_sentence():
    """「〜こと」「〜点」で終わる断片は、何がどうなのかが分からない。原文の1文（そのまま）に広げる。"""
    res = _paper_run([("多数の態度が0.7〜0.8の間に収束していること", 5), ("規範の性質が多元的無知の解消に与える影響を指摘した点", 6)],
                     "シミュレーションで多元的無知の解消を検討した資料である。")
    pts = [c.text for s in res.sections if s.title == "主要ポイント" for c in s.claims]
    assert any("部分的に多元的無知が維持されている" in p and "0.7から0.8" in p for p in pts)
    assert any(p.endswith("指摘した点である。") for p in pts)
    assert not any(p.endswith("こと") or p.endswith("点") for p in pts)
    assert res.fidelity_replaced == 0                                          # 断片の拡張は「意味のずれ」の置換ではない


def test_overview_that_became_too_short_is_extended_with_confirmed_points():
    pts = [("本研究ではコンピュータ・シミュレーションを活用して多元的無知の解消を検討する", 2),
           ("Huang and Wen（2014）のモデルに制裁の強さと本心の表明行動を加えたモデルを分析に用いる", 3),
           ("エージェントは毎期、態度と本心の更新を行い、100期繰り返す", 4),
           ("制裁が強い規範では多元的無知がより根強く維持される可能性が示唆された", 7),
           ("本研究の貢献の1つは規範の性質の違いが多元的無知の解消に与える影響を指摘した点である", 6)]
    res = _paper_run(pts, "多元的無知を扱う資料である。")
    ov = [c.text for s in res.sections if s.title == "概要" for c in s.claims][0]
    assert len(ov) >= 150 and ov.startswith("多元的無知を扱う資料である。")
    assert any("確認済みの主要ポイントを概要に補いました" in w for w in res.warnings)


def test_sentence_that_continues_from_the_previous_page_gets_its_subject_back():
    """実PDFで確認：ページをまたぐ文の後半（「が特定の規範に対して…」）は主語が欠ける。前のブロックの文末側から補う。"""
    bl = [blk(1, 1, "本モデルは2次元セル・オートマトンの構造となっている。本モデルでは、各エージェント"),
          blk(2, 2, "が特定の規範に対して「他者に示している態度X」と「本心Y」という、0〜1の値をとる2つの変数を保有しており、更新を繰り返す。")]
    cfg = Config(ctx_size=8192).validate()
    s = Summarizer(FakeEngine(lambda *a: ""), cfg)
    s._by_id = {b.id: b for b in bl}
    fixed = s._repair_head("が特定の規範に対して「他者に示している態度X」と「本心Y」という、0〜1の値をとる2つの変数を保有しており、更新を繰り返す。", {2})
    assert fixed.startswith("各エージェントが特定の規範に対して")
    # 実PDFでは、間にページ番号と誌名のヘッダが挟まる。それらは飛ばして文の前半を探す
    bl2 = [bl[0], blk(2, 2, "AAOS Transactions 2022, Vol. 11, No. 1, pp. 183-189."), blk(3, 2, "185"), blk(4, 2, bl[1].text)]
    s._by_id = {b.id: b for b in bl2}
    assert s._repair_head(bl[1].text, {4}).startswith("各エージェントが")
    s._by_id = {b.id: b for b in bl}
    # 前のブロックが無い・助詞で始まらない文は、そのまま
    assert s._repair_head("エージェントは更新を繰り返す。", {2}) == "エージェントは更新を繰り返す。"
    s._by_id = {2: bl[1]}
    assert s._repair_head("が特定の規範に対して値を保有しており、更新する。", {2}).startswith("が")


def test_hidden_count_comes_with_a_breakdown_by_reason():
    """「N件 表示していません」だけでは、何が厳しすぎるのか分からない。理由別の内訳を添える。"""
    res = _paper_run([("本研究ではコンピュータ・シミュレーションを活用して多元的無知の解消を検討する", 2),
                      ("エージェントは毎期、態度と本心の更新を行い、9999期繰り返す", 4)],
                     "シミュレーションで多元的無知の解消を検討した資料である。")
    msg = [w for w in res.warnings if "表示していません" in w]
    assert res.unverified_dropped >= 1 and msg and "内訳：" in msg[0]
    assert "原文に無い数値を含む記述" in msg[0] or "根拠を原文で見つけられない要点" in msg[0]


DEF_SRC = ("多元的無知とは、「集団の多くの成員が、自らは集団規範を受け入れていないにもかかわらず、"
           "他の成員のほとんどがその規範を受け入れていると信じている状況」を意味する（Jin,2009;Katz&Allport,1931）。")


def _definition_run(point, overview):
    bl = [blk(1, 1, "働き方改革の進展により、多くの企業でワーク・ライフ・バランスを推進する人事施策が実施されている。" * 2),
          blk(2, 1, DEF_SRC + "WLBでは、育児休暇や定時退社に対し、集団成員の多くが本心では肯定的だが、互いの信念を誤認している。"),
          blk(3, 2, "本研究ではエージェント・ベースド・シミュレーションを活用して、多元的無知の解消を検討する。" * 2)]

    def responder(system, user, n):
        if is_map(user):
            return f"- {point} [B2]" if 2 in set(ids_in(user)) else "- 要点なし"
        return overview

    cfg = Config(ctx_size=8192, fast_input_tokens=1200, chunk_tokens=120, single_pass_tokens=10,
                 reduce_input_tokens=3500, extractive_reduce=True).validate()
    return Summarizer(FakeEngine(responder), cfg).run(bl, mode="detail", kind="general")


def test_definitions_are_quoted_not_paraphrased():
    """実PDFで確認：定義の言い換えで「自らは受け入れていない」「と信じている」が抜けた。定義の文は原文の文をそのまま使う。"""
    res = _definition_run("多元的無知は、集団の多くの成員が自らの信念を誤認しているにもかかわらず、他の成員がその規範を受け入れている状況を意味する",
                          "背景として、多元的無知とは、集団の多くの成員が自らの信念を誤認しているにもかかわらず、他の成員がその規範を受け入れている状況を指す。"
                          "研究ではシミュレーションを用いて多元的無知の解消を検討した。")
    pts = [c.text for s in res.sections if s.title == "主要ポイント" for c in s.claims]
    ov = [c.text for s in res.sections if s.title == "概要" for c in s.claims][0]
    assert any("自らは集団規範を受け入れていない" in p and "と信じている" in p for p in pts)
    assert "自らの信念を誤認" not in "".join(pts)
    assert "自らは集団規範を受け入れていない" in ov and "自らの信念を誤認" not in ov
    assert any("定義の言い換え" in w for w in res.warnings)
