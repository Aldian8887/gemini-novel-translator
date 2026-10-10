"""Regression tests: format alignment M2 (investigasi 10 Okt 2026).

KASUS: "It was a <b>novel</b> poison, tasteless and patient." ->
model mengisi slot run "novel" dengan "racun" (positional slot filling)
dan menghilangkan "baru". Investigasi membuktikan (request/response mentah
+ cache DB benchmark):
  - ekstraksi run benar (3 run, format "p/b" pada run tengah),
  - instruksi "read consecutive runs together" terkirim,
  - model TETAP mengisi slot secara posisional/terisolasi,
  - klarifikasi prompt diuji dan TERBUKTI tidak mengubah perilaku.

Kesimpulan jujur: bukan bug aplikasi; keterbatasan arsitektural
(slot posisional tak bisa memindahkan emphasis saat urutan kata berubah)
+ perilaku model. Tidak ada perubahan kode produksi dari investigasi ini.

Tes-tes ini offline (tanpa API): mem-pin garansi struktural pipeline
agar tidak regresi diam-diam, dan mendokumentasikan keterbatasan.
"""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novel_translator.epub import EpubBook
from novel_translator.models import TranslationOptions, validate_response_partial
from novel_translator.token_budget import PromptBuilder


def _epub(path: Path, body_html: str):
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n<package xmlns='http://www.idpf.org/2007/opf' "
           "version='3.0' unique-identifier='uid'>\n<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>\n"
           "<dc:title>T</dc:title><dc:language>en</dc:language>\n"
           "<dc:identifier id='uid'>t</dc:identifier>\n</metadata>\n"
           "<manifest><item id='ch' href='c.xhtml' media-type='application/xhtml+xml'/></manifest>\n"
           "<spine><itemref idref='ch'/></spine>\n</package>")
    container = ("<?xml version='1.0' encoding='utf-8'?>\n<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' "
                 "version='1.0'>\n<rootfiles>\n<rootfile full-path='OEBPS/content.opf' "
                 "media-type='application/oebps-package+xml'/>\n</rootfiles>\n</container>")
    xhtml = ("<?xml version='1.0' encoding='utf-8'?>\n<html xmlns='http://www.w3.org/1999/xhtml'>\n"
             f"<head><title>T</title></head>\n<body>\n{body_html}\n</body>\n</html>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/c.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    return path


@pytest.fixture()
def m2_book(tmp_path):
    p = _epub(tmp_path / "m2.epub",
              "<p>It was a <b>novel</b> poison, tasteless and patient.</p>")
    return EpubBook(p)


def test_ekstraksi_tiga_run_dengan_format(m2_book):
    unit = next(u for u in m2_book.units if len(u.runs) == 3)
    texts = [r.text for r in unit.runs]
    assert texts == ["It was a", "novel", "poison, tasteless and patient."]
    assert unit.runs[1].style == "p/b"
    assert unit.runs[0].style == "p"


def test_payload_memuat_format_dan_instruksi_runtut(m2_book):
    builder = PromptBuilder(TranslationOptions())
    unit = next(u for u in m2_book.units if len(u.runs) == 3)
    payload = builder.unit_payload(unit)
    assert payload["runs"][1]["format"] == "p/b"
    body = builder.body([unit])
    assert "Read consecutive runs together" in body["systemInstruction"]["parts"][0]["text"]


def test_validasi_menolak_run_kosong_dan_injeksi_html(m2_book):
    from novel_translator.gemini_client import GeminiTranslator
    unit = next(u for u in m2_book.units if len(u.runs) == 3)
    rids = [r.id for r in unit.runs]
    # v2.2.4 partial salvage: run kosong ditandai bad, run valid tetap selamat
    empty = [{"id": unit.id, "runs": [
        {"id": rids[0], "text": "Itu adalah"},
        {"id": rids[1], "text": "  "},
        {"id": rids[2], "text": "racun."}]}]
    good, bad, _ = validate_response_partial([unit], empty)
    assert rids[1] in bad and rids[1] not in good
    assert rids[0] in good and rids[2] in good
    # injeksi tag HTML ditolak di lapisan klien (_validated_response),
    # bukan di validate_response_partial
    parsed = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": '[{"id": "%s", "runs": [{"id": "%s", "text": "<i>x</i>"}]}]'
                  % (unit.id, rids[0])}]}}]}
    with pytest.raises(Exception):
        GeminiTranslator._validated_response(None, [unit], parsed)


def test_simulasi_respons_model_posisional_tetap_valid_struktural(m2_book):
    """Mendokumentasikan perilaku nyata: model mengembalikan 'racun' untuk
    slot 'novel'. Pipeline harus: menerima tanpa crash, tidak menduplikasi,
    menaruh teks tepat di slotnya (bold tetap di posisi sumber)."""
    unit = next(u for u in m2_book.units if len(u.runs) == 3)
    rids = [r.id for r in unit.runs]
    # persis seperti respons benchmark M2 (dari cache DB): "novel" -> "racun"
    parsed = [{"id": unit.id, "runs": [
        {"id": rids[0], "text": "Itu adalah"},
        {"id": rids[1], "text": "racun"},
        {"id": rids[2], "text": ", tanpa rasa dan sabar."}]}]
    good, bad, _ = validate_response_partial([unit], parsed)
    assert not bad and len(good) == 3
    # tidak ada duplikasi antar run
    assert len(set(good.values())) == 3


def test_format_lain_tidak_memecah_ekstraksi(tmp_path):
    # Desain: "a" termasuk BLOCKS -> teks hyperlink menjadi unit tersendiri
    # (diterjemahkan tanpa konteks kalimat). bold/italic tetap inline.
    p = _epub(tmp_path / "mix.epub",
              "<p>The <i>cold</i> wind cut through <b>old</b> stone, "
              "see <a href=\"https://example.com\">the chronicle</a>.</p>")
    book = EpubBook(p)
    para = next(u for u in book.units
                if any(r.style == "p/b" for r in u.runs))
    styles = [r.style for r in para.runs]
    assert "p/i" in styles and "p/b" in styles
    link_units = [u for u in book.units
                  if any(r.style == "p/a" for r in u.runs)]
    assert len(link_units) == 1
    assert [r.text for r in link_units[0].runs] == ["the chronicle"]
    # prompt memuat semua run paragraf + unit link dengan formatnya
    builder = PromptBuilder(TranslationOptions())
    payload = builder.unit_payload(para)
    assert len(payload["runs"]) == len(para.runs)
    assert payload["runs"][1]["format"] == "p/i"
