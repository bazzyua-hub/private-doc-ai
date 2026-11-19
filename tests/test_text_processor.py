from app.pdf.extractor import Block
from app.pdf.text_processor import (
    block_priority,
    chunk_blocks,
    clean_blocks,
    detect_doc_kind,
    estimate_tokens,
    is_noise,
    normalize_text,
    select_for_fast,
    split_to_limit,
    truncate_text,
)

from helpers import blk

JP = "データベースの正規化では冗長性を減らして整合性を保つことが重要である。" * 12   # 約400文字


# ------------------------------------------------------------ 整形

def test_normalize_joins_japanese_lines_without_space():
    assert normalize_text("データベースは\n重要です。") == "データベースは重要です。"


def test_normalize_english_hyphenation_and_spaces():
    assert normalize_text("The data-\nbase is\nuseful.") == "The database is useful."


def test_normalize_keeps_bullet_structure():
    assert normalize_text("・正規化\n・インデックス\n・トランザクション") == "・正規化\n・インデックス\n・トランザクション"


def test_normalize_removes_invisible_chars():
    assert normalize_text("a​b c") == "ab c"


def test_noise_detection():
    for t in ["12", "- 3 -", "Page 3 / 10", "p.4", "5/20", "x"]:
        assert is_noise(t), t
    for t in ["2026年の結果", "Table 1", "12 files"]:
        assert not is_noise(t), t


def test_repeated_headers_and_footers_removed_but_body_kept():
    blocks = []
    i = 1
    for page in range(1, 6):
        blocks.append(blk(i, page, f"第{page}回 データベース論 2026", y0=20)); i += 1
        blocks.append(blk(i, page, f"{page}ページ目の本文です。" + JP[:60], y0=300)); i += 1
        blocks.append(blk(i, page, f"- {page} -", y0=810)); i += 1
    res = clean_blocks(blocks)
    texts = [b.text for b in res.blocks]
    assert not any("データベース論" in t for t in texts)
    assert not any(t.startswith("-") for t in texts)
    assert len(texts) == 5 and all("本文です" in t for t in texts)


def test_repeated_text_in_body_is_not_removed():
    # ヘッダー/フッターではない（本文の位置にある）ので、離れたページの同じ文は残す
    blocks = [blk(i, 1 + 3 * (i - 1), "同じ文言の本文ブロック", y0=300) for i in range(1, 6)]
    assert len(clean_blocks(blocks).blocks) == 5


# ------------------------------------------------------------ 見出し・種類・参考文献

def test_heading_by_font_size_and_keyword():
    blocks = [
        blk(1, 1, "第1章 データベース入門", size=20, y0=80),
        blk(2, 1, JP[:100], size=10, y0=120),
        blk(3, 1, JP[:100], size=10, y0=200),
        blk(4, 2, "Abstract", size=10, y0=80),
        blk(5, 2, JP[10:110], size=10, y0=120),
    ]
    res = clean_blocks(blocks)
    by_id = {b.id: b for b in res.blocks}
    assert by_id[1].is_heading and by_id[4].is_heading
    assert not by_id[2].is_heading and not by_id[5].is_heading


def test_paper_detection_and_reference_dropping():
    names = ["Abstract", "Introduction", "Methods", "Results", "References"]
    blocks, i = [], 1
    for p, name in enumerate(names, start=1):
        blocks.append(blk(i, p, name, size=14, y0=80)); i += 1
        blocks.append(blk(i, p, f"{name}の本文です。" + JP[:80], size=10, y0=130)); i += 1
    blocks.append(blk(i, 6, "Appendix A", size=14, y0=80)); i += 1
    blocks.append(blk(i, 6, "付録の本文です。" + JP[:80], size=10, y0=130))
    res = clean_blocks(blocks)
    assert res.doc_kind == "paper"
    texts = " ".join(b.text for b in res.blocks)
    assert "Referencesの本文" not in texts          # 参考文献の本文は除外
    assert "付録の本文" in texts                    # 付録の見出し以降は再開
    assert any("参考文献" in w for w in res.warnings)
    keep = clean_blocks(blocks, drop_references=False)
    assert "Referencesの本文" in " ".join(b.text for b in keep.blocks)


def test_general_doc_kind():
    blocks = [blk(1, 1, "講義のポイント", size=18), blk(2, 1, JP[:50]), blk(3, 2, "まとめ", size=18)]
    assert clean_blocks(blocks).doc_kind == "general"


# ------------------------------------------------------------ トークン見積り・分割

def test_estimate_tokens_is_conservative_and_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("あ" * 100) >= 100
    assert estimate_tokens("abc" * 30) < estimate_tokens("あ" * 90)
    assert estimate_tokens("12345") >= 5          # 数字は1文字ずつトークン化されやすい
    assert estimate_tokens("あ" * 200) > estimate_tokens("あ" * 100)


def test_split_to_limit_respects_limit_and_loses_nothing():
    text = JP * 5
    pieces = split_to_limit(text, 300)
    assert all(estimate_tokens(p) <= 300 for p in pieces)
    assert "".join(pieces) == text
    no_period = "あ" * 2000
    assert all(estimate_tokens(p) <= 300 for p in split_to_limit(no_period, 300))


def test_chunking_respects_budget_order_and_coverage():
    blocks = [blk(i, (i + 1) // 2, JP) for i in range(1, 11)]
    chunks = chunk_blocks(blocks, 1000)
    assert len(chunks) >= 4
    assert all(c.tokens <= 1000 for c in chunks)
    flat = [b.id for c in chunks for b in c.blocks]
    assert flat == list(range(1, 11))                  # 順序保持・欠落なし
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_oversized_block_is_split_but_keeps_its_id():
    chunks = chunk_blocks([blk(7, 1, JP * 6)], 600)
    assert len(chunks) > 1
    assert all(b.id == 7 for c in chunks for b in c.blocks)
    assert all(c.tokens <= 600 for c in chunks)


def test_chunk_does_not_end_with_dangling_heading():
    blocks = [blk(1, 1, JP[:300]), blk(2, 1, JP[:300]), blk(3, 1, "次の章", size=18, heading=True), blk(4, 1, JP[:300])]
    chunks = chunk_blocks(blocks, 720)
    for c in chunks:
        if len(c.blocks) > 1:
            assert not c.blocks[-1].is_heading
    assert any(c.blocks[0].id == 3 for c in chunks)


def test_chunk_records_preceding_heading():
    blocks = [blk(1, 1, "概要", heading=True, size=18), blk(2, 1, JP), blk(3, 2, JP), blk(4, 2, JP)]
    chunks = chunk_blocks(blocks, 900)
    assert len(chunks) >= 2
    assert chunks[1].heading == "概要"


# ------------------------------------------------------------ 高速モードの選択

def test_select_for_fast_budget_and_priority():
    blocks = []
    for i in range(1, 41):
        is_h = i % 10 == 1
        blocks.append(blk(i, (i + 3) // 4, f"見出し{i}" if is_h else JP[:150] + (f" 精度は{i}.5%" if i % 7 == 0 else ""),
                          size=18 if is_h else 10, heading=is_h))
    chosen = select_for_fast(blocks, budget_tokens=800, block_chars=120)
    assert [b.id for b in chosen] == sorted(b.id for b in chosen)         # 文書順
    assert sum(estimate_tokens(b.text) + 6 for b in chosen) <= 800
    assert {1, 11, 21, 31} <= {b.id for b in chosen}                       # 見出しは優先して残る
    assert all(len(b.text) <= 121 for b in chosen)                         # 長いブロックは切り詰め
    assert len(chosen) < len(blocks)


def test_truncate_prefers_sentence_boundary():
    t = "最初の文です。" * 3 + "途中で切れる長い文" * 10
    out = truncate_text(t, 25)
    assert out.endswith("…") and "。" in out


def test_priority_scores_headings_and_numbers_higher():
    blocks = [blk(1, 1, "見出し", size=18, heading=True), blk(2, 1, "短い"), blk(3, 5, JP[:80]),
              blk(4, 5, JP[:80] + "は95.2%"), blk(5, 9, JP[:80])]
    s = block_priority(blocks)
    assert s[1] > s[3] and s[4] > s[3] and s[2] < s[3]


def test_select_for_fast_prefers_body_text_over_headings_and_spreads_pages():
    # 1ページ = 見出し + 短い見出し + 本文、のような構成（実PDFで見出しばかり選ばれた事例）
    blocks, i = [], 1
    for page in range(1, 21):
        blocks.append(blk(i, page, f"{page:02d} 見出し", size=18, heading=True)); i += 1
        blocks.append(blk(i, page, "概要", size=14, heading=True)); i += 1
        blocks.append(blk(i, page, f"本文{page}: " + JP[:90])); i += 1
    chosen = select_for_fast(blocks, budget_tokens=900, block_chars=120)
    heads = [b for b in chosen if b.is_heading]
    bodies = [b for b in chosen if not b.is_heading]
    assert len(bodies) > len(heads)                                    # 本文が主役
    assert len({b.page for b in bodies}) >= len(bodies) * 0.8          # ページが偏らない
    assert sum(estimate_tokens(b.text) + 6 for b in chosen) <= 900
    assert sum(1 for b in heads if b.text == "概要") <= 1              # 同じ文言の見出しは繰り返さない
    # 見出しは、選んだ本文の直前のものだけ
    ids = {b.id for b in chosen}
    for h in heads:
        assert any(h.id + k in ids and not next(x for x in blocks if x.id == h.id + k).is_heading for k in (1, 2))


# ------------------------------------------------------------ スライド資料（アニメーション送り・定型ページ）

def test_identical_blocks_on_consecutive_pages_are_collapsed_but_distant_ones_kept():
    def slide(first_id, page, body):
        return [blk(first_id, page, "3 ステップ構成", size=28, y0=60, heading=True),
                blk(first_id + 1, page, body, size=24, y0=200)]
    body = "◦学習時の特徴量を算出する◦データを空間へ写像する"
    blocks = slide(1, 4, body) + slide(3, 5, body) + slide(5, 6, body + "◦決定境界を決める") + slide(7, 12, body)
    out = clean_blocks(blocks).blocks
    assert [b.id for b in out if b.text == "3 ステップ構成"] == [1, 7]        # 連続3ページ分は先頭だけ、離れたp.12は残る
    assert [b.id for b in out if b.text == body] == [2, 8]
    assert any(b.id == 6 for b in out)                                      # 内容が増えた段階は残す


def test_boilerplate_sections_are_deprioritised():
    blocks = [blk(1, 1, "スケジュール(予定)", size=44, heading=True), blk(2, 1, "第1 回：イントロ 第2 回：数学 " * 3),
              blk(3, 2, "畳み込み層", size=44, heading=True), blk(4, 2, "◦局所的な特徴を抽出する" * 4)]
    s = block_priority(blocks)
    assert s[2] < s[4] and s[1] < s[3]


def test_select_for_fast_skips_tiny_fragments():
    blocks = [blk(1, 1, "6.", size=28), blk(2, 1, "確認結果"), blk(3, 1, JP[:80]), blk(4, 2, JP[10:90])]
    chosen = {b.id for b in select_for_fast(blocks, budget_tokens=800, block_chars=120)}
    assert chosen == {3, 4}
    only_tiny = select_for_fast([blk(1, 1, "6.")], budget_tokens=800, block_chars=120)
    assert [b.id for b in only_tiny] == [1]                      # それしか無いときは諦めて使う


def test_schedule_table_is_dropped_but_ordinary_tables_stay():
    sched = "回数\n日付\n主担当\n内容\n" + "".join(f"第{i:02d}回\n{9 + i // 4}/{10 + i}\n寺田\n演習{i}\n" for i in range(1, 10))
    data = "区分\n人数\n割合\n" + "".join(f"区分{i}\n{100 + i}\n{i}.5%\n" for i in range(1, 6))
    res = clean_blocks([blk(1, 1, sched, size=12), blk(2, 2, data, size=12), blk(3, 2, JP[:80], size=10, y0=300)])
    ids = {b.id for b in res.blocks}
    assert 1 not in ids and 2 in ids and 3 in ids
    assert any("日程表" in w for w in res.warnings)


def test_course_admin_block_is_deprioritised():
    blocks = [blk(1, 1, "担当：寺田真敏\n曜日・時限：木曜4限\n場所：2号館", size=18),
              blk(2, 1, "アクセス制御の設定を行い、反映後に動作を確認する演習である" * 2, size=18)]
    s = block_priority(blocks)
    assert s[1] < s[2]


# ------------------------------------------------------------ 作業を求める文・コマンド例

def test_is_action_block_detects_tasks_and_numbered_steps_but_not_plain_explanations():
    from app.pdf.text_processor import is_action_block
    yes = [
        "⚫【課題2】次の操作を実施し、設定を反映しなさい。",
        "※ 【課題1】の記載終了後、(1)グループメンバ同士で確認後、(2)副手の記載チェックを受けること。",
        "①管理者権限での操作に切り替える。",
        "②access.confファイルを作成する。",
        "次の条件を満たすよう設定表を作成せよ。",
        "提出前に必ず誤字を確認してください。",
    ]
    no = [
        "【課題1】(続き)",
        "【課題】Webサーバのアクセス制御設定",
        "⚫ 【課題1】httpdプログラムでのアクセス制御設定表の作成",
        "指定されたディレクトリにおいては、すべてのアクセスを受け入れる。",
        "ブラウザのキャッシュを回避することができる。",
        "トラブルシューティングの進め方",
    ]
    assert all(is_action_block(t) for t in yes), [t for t in yes if not is_action_block(t)]
    assert not any(is_action_block(t) for t in no), [t for t in no if is_action_block(t)]


def test_is_code_block_detects_shell_sessions_and_config_but_not_japanese_prose():
    from app.pdf.text_processor import is_code_block
    assert is_code_block("[root@L198]# /sbin/service httpd restart")
    assert is_code_block("ゲストOSコンソール\n[tduser@iot]$ whoami\ntduser")
    assert is_code_block('<Directory "/var/sample_public">\nRequire all granted\n</Directory>')
    assert not is_code_block("管理者権限での操作に切り替える。")
    assert not is_code_block("2026年度の売上は前年より増加した。")


def test_action_blocks_get_priority_and_consecutive_steps_are_selected_from_one_page():
    from app.pdf.text_processor import is_action_block
    blocks = [blk(1, 1, "概要と背景についての長い説明文である。" * 4, heading=False)]
    steps = ["①管理者権限での操作に切り替える。", "②設定ファイルを作成する。", "③変更を反映する。", "④管理者権限での操作を終了する。"]
    blocks += [blk(2 + i, 5, t) for i, t in enumerate(steps)]
    blocks += [blk(10 + i, 2 + i, "背景の説明として別の話題を述べる文章である" + "あいうえおかきくけこ"[i] * 3 + "。" * 1) for i in range(5)]
    sel = select_for_fast(blocks, budget_tokens=120, block_chars=200)
    got = {b.id for b in sel}
    assert {2, 3, 4, 5} <= got                       # ①〜④が全部入る（同じページでも減点しない）
    scores = block_priority(blocks)
    assert scores[2] > scores[10]


def test_page_numbered_footer_is_removed_even_on_the_page_where_it_has_no_number():
    footer = "Tokyo Denki University, 2026."
    blocks = []
    for pg in range(1, 7):
        blocks.append(blk(pg * 10, pg, JP[:60] + f"ページ{pg}の本文の違い。"))
        text = footer if pg == 1 else f"{190 + pg}\n{footer}"
        blocks.append(blk(pg * 10 + 1, pg, text, y0=800.0))
    res = clean_blocks(blocks)
    assert not any("Tokyo Denki" in b.text for b in res.blocks)


def test_numbered_lines_are_steps_only_when_they_end_like_an_action():
    from app.pdf.text_processor import is_action_block
    assert is_action_block("④ ブラウザから再度アクセスすると、該当するログが表示される。")
    assert is_action_block("①設定を把握する\n②決定する\n③反映する")                      # 番号が3つ以上なら手順
    assert not is_action_block("①売上が増加した。")
    assert not is_action_block("①本件は重大な問題である。")
