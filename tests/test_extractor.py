import pymupdf
import pytest

from app.core.errors import PdfError
from app.pdf.extractor import _Raw, _reading_order, extract_pdf

from helpers import make_pdf, make_two_column_pdf

PARA_A = "データベースとは、データを一定の形式で整理して保存し、検索や更新ができるようにした仕組みである。"
PARA_B = "正規化によって冗長性を減らし、整合性を保つ。第三正規形までが実務ではよく使われる。"


def test_extracts_text_pages_and_ids(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", [[PARA_A, PARA_B], [PARA_B]])
    res = extract_pdf(pdf)
    assert res.page_count == 2
    assert [b.page for b in res.blocks] == [1, 1, 2]
    assert [b.id for b in res.blocks] == [1, 2, 3]            # 文書全体で連番
    assert "データベース" in res.blocks[0].text
    assert res.total_chars > 50
    assert res.elapsed_s >= 0


def test_two_column_reading_order(tmp_path):
    res = extract_pdf(make_two_column_pdf(tmp_path / "two.pdf"))
    texts = [b.text for b in res.blocks]
    first = [t.split()[0] for t in texts]
    # タイトル → 左段3つ → 右段3つ
    assert first[0] == "A"
    assert first[1:4] == ["Left"] * 3
    assert first[4:7] == ["Right"] * 3


def test_reading_order_reorders_interleaved_columns():
    """内容ストリーム順が左右交互になっている2段組ページを、左段→右段の順に直す。"""
    def raw(x0, x1, y0, name):
        return _Raw(x0, y0, x1, y0 + 80, name, 10.0)

    blocks = [
        raw(50, 545, 40, "title"),
        raw(310, 545, 120, "R1"), raw(50, 290, 120, "L1"),
        raw(310, 545, 230, "R2"), raw(50, 290, 230, "L2"),
    ]
    assert [b.text for b in _reading_order(blocks, 595.0)] == ["title", "L1", "L2", "R1", "R2"]


def test_reading_order_keeps_single_column_pages_untouched():
    blocks = [_Raw(50, 545, y, y + 40, f"p{i}", 10.0) for i, y in enumerate((100, 200, 300, 400))]
    assert _reading_order(blocks, 595.0) == blocks


def test_corrupt_file_raises_pdferror(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not really a pdf \x00\x01\x02")
    with pytest.raises(PdfError):
        extract_pdf(bad)


def test_non_pdf_and_missing_file(tmp_path):
    txt = tmp_path / "x.pdf"
    txt.write_text("hello")
    with pytest.raises(PdfError):
        extract_pdf(txt)
    with pytest.raises(PdfError):
        extract_pdf(tmp_path / "missing.pdf")


def test_encrypted_pdf_is_rejected(tmp_path):
    src = make_pdf(tmp_path / "plain.pdf", [[PARA_A]])
    doc = pymupdf.open(str(src))
    enc = tmp_path / "enc.pdf"
    doc.save(str(enc), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()
    with pytest.raises(PdfError, match="パスワード"):
        extract_pdf(enc)


def test_image_only_pdf_has_no_text(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    p = tmp_path / "blank.pdf"
    doc.save(str(p))
    with pytest.raises(PdfError, match="文字情報"):
        extract_pdf(p)


def test_mostly_blank_pdf_warns(tmp_path):
    pdf = make_pdf(tmp_path / "w.pdf", [[PARA_A], [], [], []])
    res = extract_pdf(pdf)
    assert any("画像主体" in w for w in res.warnings)


def test_limits(tmp_path):
    pdf = make_pdf(tmp_path / "l.pdf", [[PARA_A]] * 3)
    with pytest.raises(PdfError, match="ページ数"):
        extract_pdf(pdf, max_pages=2)
    with pytest.raises(PdfError, match="大きすぎ"):
        extract_pdf(pdf, max_mb=0.000001)
    res = extract_pdf(pdf, max_chars_per_page=20)
    assert all(len(b.text) <= 20 for b in res.blocks)
    assert any("打ち切り" in w for w in res.warnings)


# ------------------------------------------------------------ 行ごとに別ブロックのPDFを段落にまとめる

def _raw(y0, text, *, x0=50.0, size=10.0, x1=500.0):
    from app.pdf.extractor import _Raw
    return _Raw(x0, y0, x1, y0 + size, text, size)


def test_merge_lines_joins_consecutive_lines_of_one_paragraph():
    from app.pdf.extractor import _merge_lines
    raws = [_raw(100, "網膜上で大きくなる物体像は、物体が接近している"), _raw(119, "可能性を示す視覚情報である。"),
            _raw(138, "人を対象にした研究では、接近刺激に対して")]
    out = _merge_lines(raws)
    assert len(out) == 1 and out[0].text.count("\n") == 2
    assert out[0].y0 == 100 and out[0].y1 == 148


def test_merge_lines_keeps_paragraph_gaps_bullets_headings_and_size_changes():
    from app.pdf.extractor import _merge_lines
    assert len(_merge_lines([_raw(100, "段落の最後の行である。"), _raw(130, "次の段落の最初の行である。")])) == 2   # 行間が広い
    assert len(_merge_lines([_raw(100, "前の文である。"), _raw(112, "・箇条書きの項目")])) == 2
    assert len(_merge_lines([_raw(100, "前の文である。"), _raw(112, "2. 目的と仮説")])) == 2
    assert len(_merge_lines([_raw(100, "2. 目的と仮説"), _raw(112, "本文の最初の行である。")])) == 2
    assert len(_merge_lines([_raw(100, "見出し", size=18), _raw(124, "本文の行である。", size=10)])) == 2
    assert len(_merge_lines([_raw(100, "左の列の文", x0=50, x1=250), _raw(112, "右の列の文", x0=300, x1=500)])) == 2


def test_merge_lines_does_not_create_giant_blocks():
    from app.pdf.extractor import _merge_lines
    raws = [_raw(100 + 11 * i, "あ" * 100) for i in range(20)]
    out = _merge_lines(raws)
    assert len(out) >= 3 and all(len(r.text) <= 900 for r in out)
