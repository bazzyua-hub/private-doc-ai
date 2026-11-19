"""CLI を通した結合テスト（偽の llama-server を使用）。PDF抽出 → エンジン → 要約 → 表示 → 計測 の一連を通す。"""
import json
import stat
import sys
from pathlib import Path

import pytest

import main as cli
from helpers import make_pdf

FAKE = Path(__file__).parent / "fake_llama_server.py"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="偽サーバーの起動ラッパーは POSIX 用")

PARA = "データベースとは、データを一定の形式で整理して保存し、検索や更新ができるようにした仕組みである。"


@pytest.fixture
def env(tmp_path):
    wrapper = tmp_path / "llama-server"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "Qwen3-1.7B-Q8_0.gguf"
    model.write_bytes(b"GGUF")
    pdf = make_pdf(tmp_path / "doc.pdf", [["# 第1章 データベース", PARA], [PARA]], header="講義資料 {n}", footer=True)
    return pdf, wrapper, model


REPLY = ("## 概要\n- データベースは、データを一定の形式で整理して保存し、検索や更新ができるようにした仕組みである。\n\n"
         "## 主要ポイント\n- データを整理して保存し、検索や更新ができるようにした仕組みがデータベースである\n")


@posix_only
def test_cli_summarize_end_to_end(env, capsys, monkeypatch):
    pdf, server, model = env
    monkeypatch.setenv("FAKE_REPLY", REPLY)
    code = cli.main([str(pdf), "--llama-server", str(server), "--model", str(model), "--sources"])
    out, err = capsys.readouterr()
    assert code == 0
    assert "データを整理して保存し" in out and "参照した原文" in out        # 偽サーバーの応答（原文に基づく）が表示される
    assert "## やること・手順" in out and "該当なし" in out                 # 作業指示が無い資料は「該当なし」
    assert "処理上の注意" in out and "高速モード" in out
    assert "入力処理速度" in err and "出力速度" in err and "モデル起動" in err


@posix_only
def test_cli_reports_an_error_when_nothing_in_the_model_output_can_be_confirmed_in_the_document(env, capsys):
    pdf, server, model = env                                         # 既定の応答「こんにちは世界。」は、原文に無い
    code = cli.main([str(pdf), "--llama-server", str(server), "--model", str(model)])
    out, err = capsys.readouterr()
    assert code != 0 and "確認できた記述がありません" in err + out


@posix_only
def test_cli_json_output(env, capsys, monkeypatch):
    pdf, server, model = env
    monkeypatch.setenv("FAKE_REPLY", REPLY)
    assert cli.main([str(pdf), "--llama-server", str(server), "--model", str(model), "--json", "--mode", "detail"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "detail" and data["pages"] == 2 and data["sections"]


def test_cli_extract_only_needs_no_llm(env, capsys):
    pdf, _, _ = env
    assert cli.main([str(pdf), "--extract-only"]) == 0
    err = capsys.readouterr().err
    assert "ページ数        : 2" in err and "推定トークン数" in err


def test_cli_reports_broken_pdf_in_japanese(tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert cli.main([str(bad), "--extract-only"]) == 1
    assert "エラー:" in capsys.readouterr().err


def test_cli_missing_model_and_server_messages(env, capsys, tmp_path, monkeypatch):
    pdf, _, _ = env
    monkeypatch.setattr(cli, "Config", lambda: __import__("app.core.config", fromlist=["Config"]).Config(
        models_dir=tmp_path / "nomodels", llama_dir=tmp_path / "nollama"))
    monkeypatch.delenv("LOCALDOC_LLAMA_SERVER", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli.main([str(pdf)]) == 1
    err = capsys.readouterr().err
    assert "setup_assets.py" in err


@posix_only
def test_cli_check(env, capsys):
    _, server, model = env
    assert cli.main(["--check", "--llama-server", str(server), "--model", str(model)]) == 0
    err = capsys.readouterr().err
    assert "必須引数      : OK" in err and "実行できる状態" in err


def test_cli_rejects_bad_config(env, capsys):
    pdf, _, _ = env
    assert cli.main([str(pdf), "--ctx", "1024"]) == 2
    assert "設定エラー" in capsys.readouterr().err
