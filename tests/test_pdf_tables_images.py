import os
import re
import sys

import fitz
import pytest

os.environ.setdefault("JWT_SECRET", "test-only-not-a-real-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/x")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from routers.editor import _extract_pdf_to_html  # noqa: E402

LOREM = "Para pihak sepakat bahwa ketentuan layanan berlaku sejak tanggal penandatanganan perjanjian ini."


def _draw_table(page, y, rows):
    x0, w, h = 72, 150, 24
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            rect = fitz.Rect(x0 + c * w, y + r * h, x0 + (c + 1) * w, y + (r + 1) * h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), text, fontsize=10)


def _make_pdf(path, tiny_images=0):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 60), "PASAL 1 TARIF", fontsize=14)
    p.insert_text((72, 90), LOREM, fontsize=10)
    _draw_table(p, 120, [["Layanan", "Harga"], ["Internet", "100"], ["Cloud", "200"]])
    p.insert_text((72, 260), LOREM, fontsize=10)
    # vector-drawn signature scribble
    p.draw_bezier((80, 330), (110, 290), (140, 370), (190, 320), color=(0, 0, 1), width=1.5)
    p.draw_bezier((190, 320), (210, 300), (230, 360), (260, 330), color=(0, 0, 1), width=1.5)
    p.insert_text((80, 400), "Budi Santoso", fontsize=10)
    for i in range(tiny_images):
        p.insert_image(fitz.Rect(10 + i, 10, 14 + i, 14), pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False))
    doc.save(path)


def test_table_and_signature_extracted(tmp_path):
    pdf = tmp_path / "a.pdf"
    _make_pdf(str(pdf))
    out = _extract_pdf_to_html(str(pdf))
    assert out.count('<table class="ed-doc-table">') == 1
    assert "<th>Layanan</th>" in out and "<td>Cloud</td>" in out
    assert out.count("Cloud") == 1  # not duplicated as loose text
    assert out.count("<img") == 1 and "data:image/jpeg;base64," in out
    assert out.index("PASAL 1") < out.index("<table") < out.index("<img") < out.index("Budi Santoso") or \
        out.index("<img") < out.index("Budi Santoso")


def test_tiny_images_ignored_and_no_table_pdf(tmp_path):
    pdf = tmp_path / "b.pdf"
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 80), LOREM, fontsize=10)
    for i in range(30):
        p.insert_image(fitz.Rect(10 + i * 5, 10, 14 + i * 5, 14), pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False))
    doc.save(str(pdf))
    out = _extract_pdf_to_html(str(pdf))
    assert "<table" not in out and "<img" not in out


def test_scanned_sample_does_not_crash():
    sample = os.path.join(os.path.dirname(__file__), "..", "..", "la-legpro-doc", "data example", "PKS - PT Indivara Sejahtera Sukses Makmur.pdf")
    if not os.path.exists(sample):
        pytest.skip("sample absent")
    assert _extract_pdf_to_html(sample) == ""  # scanned: no text layer, VLM path takes over
