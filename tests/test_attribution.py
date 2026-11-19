from app.summary.attribution import Attributor

from helpers import blk

A = "第三正規形まで進めると更新時の不整合を防げるため、冗長なデータが約17.5%減少した。"
B = "B木インデックスは範囲検索に強く、検索時間が大幅に短縮される。更新が多い表では書き込みが遅くなる。"
C = "分散システムではCAP定理により、ネットワーク分断時に一貫性か可用性のどちらかを選ぶ必要がある。"


def make():
    return Attributor([blk(1, 1, A), blk(2, 1, B), blk(3, 2, C),
                       blk(4, 2, "本資料は社内向けの説明です。ご質問があれば担当者までお知らせください。")])


def test_finds_the_block_that_a_paraphrased_claim_came_from():
    assert make().attribute("第三正規形で更新時の不整合を防げる") == [1]
    assert make().attribute("ネットワーク分断時は一貫性か可用性のどちらかを選ぶ") == [3]


def test_claim_spanning_two_blocks_returns_both():
    ids = make().attribute("B木インデックスは範囲検索に強い。ネットワーク分断時は一貫性か可用性のどちらかを選ぶ")
    assert ids == [2, 3]


def test_unrelated_claim_is_not_attributed():
    assert make().attribute("宇宙ステーションの軌道高度は約400キロメートルである") == []


def test_too_short_claim_is_not_attributed():
    assert make().attribute("更新") == []
    assert make().attribute("") == []


def test_empty_document_is_safe():
    assert Attributor([]).attribute("第三正規形で更新時の不整合を防げる") == []


def test_support_distinguishes_right_and_wrong_citations():
    a = make()
    assert a.support("第三正規形で更新時の不整合を防げる", [1]) > 0.6
    assert a.support("第三正規形で更新時の不整合を防げる", [3]) < 0.2        # 別ページの番号を書いた
    assert a.support("宇宙ステーションの軌道高度は約400キロメートルである", [1]) < 0.2
    assert a.support("短い", [3]) == 1.0                                      # 短すぎる文は疑わない
