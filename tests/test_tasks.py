"""課題カード（原文からの手順抜き出し）のテスト。"""
from app.summary.schemas import SummaryResult
from app.summary.tasks import extract_task_cards

from helpers import blk


def _doc():
    """実習書の構造を縮めたもの（【課題N】の指示文・条件・①②手順・コマンド・※確認）。"""
    t = [
        # (page, text, heading)
        (1, "Webサーバの演習", True),
        (2, "⚫\n【課題1】設定表の作成\n⚫\n【課題2】設定の反映\n⚫\n【課題3】反映後の調査", False),          # 目次
        (3, "⚫【課題1】例題を参考に、次の条件を満たす設定表を作成しなさい。", False),
        (3, "⚫アクセス制御の設定は、/etc/httpd/conf.d/access.confに追記する。⚫対象ディレクトリ⚫ /var/www/html/public", False),
        (3, "【課題1】設定表の作成", True),
        (4, "※ 【課題1】の記載終了後、(1)グループメンバ同士で確認後、(2)副手の記載チェックを受けること。", False),
        (5, "⚫【課題2】次の操作を実施し、設定を反映しなさい。", False),
        (5, "ゲストOSコンソール/TeraTerm [tduser@L198]$ su", False),
        (5, "[root@L198]# /sbin/service httpd restart 192.168.1.166 - - [04/Apr/2017:23:16:47 +0900] \"GET /x HTTP/1.1\"", False),
        (5, "①管理者権限での操作に切り替える。", False),
        (5, "②設定ファイルを作成する。", False),
        (5, "③上記の変更を反映する。", False),
        (6, "HTTPのステータスコード", True),
        (6, "200 OKはアクセス成功を表す。404はページが無いことを表す。", False),
        (7, "⚫【課題3a】curlでアクセスした際のアクセスログを記録しなさい。", False),
        (7, "【課題3】反映後の調査", True),
        (8, "⚫【課題3b】ブラウザでアクセスした際のエラーログを記録しなさい。", False),
        (8, "※ 【課題3】の調査終了後、副手の記載チェックを受けること。", False),
    ]
    return [blk(i + 1, pg, tx, heading=h) for i, (pg, tx, h) in enumerate(t)]


def test_cards_hold_instruction_conditions_steps_commands_and_checks_verbatim():
    ex = extract_task_cards(_doc())
    by = {c.title: c for c in ex.cards}
    assert list(by) == ["課題1", "課題2", "課題3a", "課題3b"]

    c1 = by["課題1"]
    assert c1.topic == "設定表の作成"
    assert c1.do[0].text == "例題を参考に、次の条件を満たす設定表を作成しなさい。"        # 【課題1】の付記を除き、原文のまま
    assert any("access.conf" in c.text for c in c1.conditions)
    assert any("副手の記載チェックを受けること" in c.text for c in c1.checks)
    assert c1.do[0].block_ids == [3] and c1.do[0].status == "ok"

    c2 = by["課題2"]
    assert [c.text for c in c2.steps] == ["①管理者権限での操作に切り替える。", "②設定ファイルを作成する。", "③上記の変更を反映する。"]
    assert [c.text for c in c2.commands] == ["[tduser@L198]$ su", "[root@L198]# /sbin/service httpd restart"]   # ログ行は除く
    assert c2.place == "ゲストOSコンソール/TeraTerm"


def test_outline_and_missing_tasks_are_reported_from_the_contents_list():
    doc = _doc()
    doc[1] = blk(2, 2, "⚫\n【課題1】設定表の作成\n⚫\n【課題2】設定の反映\n⚫\n【課題3】反映後の調査\n⚫\n【課題4】総括", heading=False)
    ex = extract_task_cards(doc)
    assert ex.outline is not None and "課題4: 総括" in ex.outline.text
    assert ex.missing == ["課題4"]


def test_a_note_about_task_3_applies_to_both_3a_and_3b():
    by = {c.title: c for c in extract_task_cards(_doc()).cards}
    assert by["課題3a"].checks and by["課題3b"].checks
    assert "副手" in by["課題3b"].checks[0].text


def test_unrelated_heading_ends_the_scope_of_a_task():
    doc = _doc()
    by = {c.title: c for c in extract_task_cards(doc).cards}
    assert not any("200 OK" in c.text for cl in (by["課題2"].conditions, by["課題2"].do) for c in cl)
    # 見出し「HTTPのステータスコード」以降の番号つき手順は、課題2のものにならない
    doc2 = doc[:13] + [blk(99, 6, "①別の話の手順を実行する。", heading=False)] + doc[13:]
    by2 = {c.title: c for c in extract_task_cards(doc2).cards}
    assert len(by2["課題2"].steps) == 3


def test_numbered_steps_without_a_task_label_become_a_card_with_the_nearby_heading_as_title():
    doc = [
        blk(1, 1, "ログの確認方法", heading=True),
        blk(5, 1, "次の手順でログを確認してください。"),
        blk(2, 1, "ゲストOSコンソール/TeraTerm\n[ictuser@L198]$ su\nPassword:"),
        blk(3, 1, "①管理者権限に切り替える。"),
        blk(4, 1, "②tail -fでログを表示する。"),
    ]
    ex = extract_task_cards(doc)
    assert len(ex.cards) == 1
    c = ex.cards[0]
    assert c.labeled is False and c.title == "ログの確認方法"
    assert [x.text for x in c.steps] == ["①管理者権限に切り替える。", "②tail -fでログを表示する。"]
    assert c.commands and c.commands[0].text == "[ictuser@L198]$ su"
    assert ex.outline is None


def _steps_doc(title: str, intro: str = "", steps=("①管理者権限に切り替える。", "②tail -fでログを表示する。")):
    doc = [blk(1, 1, title, heading=True)]
    if intro:
        doc.append(blk(2, 1, intro))
    doc += [blk(10 + i, 1, t) for i, t in enumerate(steps)]
    return doc


def test_numbered_steps_are_reader_tasks_only_when_the_document_asks_the_reader_to_do_them():
    # 作業を求める文が直前にある → 読者へのやること
    assert len(extract_task_cards(_steps_doc("ログの確認方法", "次の手順でログを確認してください。")).cards) == 1
    # 題が「読者への呼びかけ」 → やること
    assert len(extract_task_cards(_steps_doc("意識して欲しいこと")).cards) == 1
    # 手順の文そのものが作業を求める → やること
    assert len(extract_task_cards(_steps_doc("ログの確認方法", steps=("①ログを開くこと。", "②結果を記録すること。"))).cards) == 1
    # 何も求めていない、ただの手順の列挙 → やることには入れない（載せなかったページを記録する）
    ex = extract_task_cards(_steps_doc("ログの確認方法"))
    assert ex.cards == [] and ex.skipped_pages == [1]


def test_experiment_procedures_and_past_tense_records_are_not_reader_tasks():
    # 実験・調査の説明（題に「実験」「調査」）や、過去形の記録は、読者への指示ではない
    assert extract_task_cards(_steps_doc("実験手順")).cards == []
    done = ("①試料を80度で加熱した。", "②温度を測定した。", "③結果を記録した。")
    assert extract_task_cards(_steps_doc("方法", "次の手順で実施した。", done)).cards == []
    # 題が実験でも、文が作業を求めていれば（実習書など）やること
    assert len(extract_task_cards(_steps_doc("実験手順", steps=("①試料を加熱すること。", "②温度を測定すること。"))).cards) == 1


def test_a_single_numbered_line_or_a_list_of_states_is_not_a_card():
    assert extract_task_cards([blk(1, 1, "①管理者権限に切り替える。")]).cards == []
    states = [blk(1, 1, "①売上が増加した。"), blk(2, 1, "②利益が減少した。")]
    assert extract_task_cards(states).cards == []


def test_no_instructions_gives_no_cards():
    doc = [blk(1, 1, "概要", heading=True), blk(2, 1, "データベースの正規化は冗長性を減らす手法である。" * 3)]
    ex = extract_task_cards(doc)
    assert ex.cards == [] and ex.outline is None and ex.missing == []


def test_render_text_lists_cards_with_pages_and_to_dict_exposes_them():
    doc = _doc()
    ex = extract_task_cards(doc)
    res = SummaryResult(mode="detail", doc_kind="general", source_name="x.pdf", page_count=8, sections=[],
                        evidence={b.id: b for b in doc}, tasks=ex.cards, task_outline=ex.outline)
    text = res.render_text()
    assert "## やること・手順（原文から抜き出し）" in text
    assert "### 課題1：設定表の作成（p.3-4）" in text
    assert "- やること: 例題を参考に、次の条件を満たす設定表を作成しなさい。" in text
    assert "- コマンド（ゲストOSコンソール/TeraTerm）:" in text and "    [root@L198]# /sbin/service httpd restart" in text
    d = res.to_dict()
    assert d["tasks"][1]["title"] == "課題2" and d["tasks"][1]["steps"][0]["pages"] == [5]
