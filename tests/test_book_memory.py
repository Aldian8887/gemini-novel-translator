"""Regression tests untuk Book Memory v0.

Menjamin: store round-trip, identitas buku stabil, deteksi konflik,
affected-units, validasi pasca-terjemahan, dan — yang terpenting —
glossary dari Book Memory menghasilkan dict IDENTIK dengan glossary manual
sehingga instruksi yang diterima model byte-identik (tidak ada klaim
peningkatan kualitas dari sekadar penyimpanan baru).
"""
import json
import re
import tempfile
from pathlib import Path

import pytest

from novel_translator.book_memory import (
    BookMemory, BookMemoryStore, GlossaryTerm, CharacterNote,
    compute_book_id, detect_conflicts, changed_terms,
    units_affected_by_change, validate_glossary, term_pattern,
)
from novel_translator.models import Run, Unit


def _unit(uid, text):
    return Unit(id=uid, document="d", context="", runs=(Run(id="r0", text=text),))


def build_mini_epub(path: Path) -> Path:
    """Bangun fixture EPUB minimal yang deterministik.

    Disertakan dalam proyek sebagai KODE (bukan blob biner) agar portabel:
    tidak ada path absolut, tidak ada dependensi biner di git.
    """
    import zipfile
    xhtml = ("<?xml version='1.0' encoding='utf-8'?>\n"
             "<html xmlns='http://www.w3.org/1999/xhtml'>\n"
             "<head><title>Fixture Book</title></head>\n<body>\n"
             "<h1>Chapter One</h1>\n"
             "<p>The Hunter met Hunter at dawn.</p>\n"
             "<p>The old bead glowed faintly on the table.</p>\n"
             "</body>\n</html>")
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
           "unique-identifier='u'>\n"
           "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<dc:title>Fixture Book</dc:title>\n"
           "<dc:language>en</dc:language><dc:identifier id='u'>fixture-1</dc:identifier>"
           "</metadata>\n"
           "<manifest><item id='c' href='c.xhtml' "
           "media-type='application/xhtml+xml'/></manifest>\n"
           "<spine><itemref idref='c'/></spine></package>")
    cont = ("<?xml version='1.0' encoding='utf-8'?>\n"
            "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' "
            "version='1.0'>\n<rootfiles><rootfile full-path='OEBPS/c.opf' "
            "media-type='application/oebps-package+xml'/></rootfiles></container>")
    path = Path(path)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", cont,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/c.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/c.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    return path


@pytest.fixture()
def mini_epub(tmp_path):
    return build_mini_epub(tmp_path / "mini_book.epub")


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield BookMemoryStore(tmp)


def test_store_round_trip(store):
    mem = BookMemory(book_id="bm_test", title="Contoh",
                     terms=[GlossaryTerm("Nobility", "kaum Nobility", True, "faksi")],
                     characters=[CharacterNote(name="D", aliases=["Dunpeal"],
                                               title="Pemburu Vampir")])
    store.save(mem)
    back = store.load("bm_test")
    assert back.title == "Contoh"
    assert back.glossary_dict() == {"Nobility": "kaum Nobility"}
    assert back.characters[0].aliases == ["Dunpeal"]


def test_load_missing_returns_empty(store):
    mem = store.load("bm_tidak_ada")
    assert mem.book_id == "bm_tidak_ada"
    assert mem.glossary_dict() == {}


def test_glossary_dict_approved_only(store):
    mem = BookMemory(book_id="x", terms=[
        GlossaryTerm("a", "A", approved=True),
        GlossaryTerm("b", "B", approved=False)])
    assert mem.glossary_dict() == {"a": "A"}
    assert mem.glossary_dict(approved_only=False) == {"a": "A", "b": "B"}


def test_book_id_stable_and_content_based(tmp_path, mini_epub):
    # dua salinan identik -> id sama; konten diubah -> id berbeda
    import shutil
    import zipfile as zf
    a = tmp_path / "a.epub"; b = tmp_path / "b.epub"
    shutil.copy(mini_epub, a); shutil.copy(mini_epub, b)
    assert compute_book_id(a) == compute_book_id(b)
    assert re.fullmatch(r"bm_[0-9a-f]{16}", compute_book_id(a))
    c = tmp_path / "c.epub"
    with zf.ZipFile(mini_epub) as zin, zf.ZipFile(c, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "OEBPS/c.xhtml":
                data = data.replace(b"Hunter", b"Hunterr", 1)
            zout.writestr(item, data)
    assert compute_book_id(c) != compute_book_id(a)


def test_detect_conflicts():
    terms = [GlossaryTerm("Nobility", "kaum Nobility"),
             GlossaryTerm("nobility ", "kaum Nobility"),   # duplikat
             GlossaryTerm("Frontier", "Frontier"),
             GlossaryTerm("Northern Frontier", "Frontier Utara"),  # overlap
             GlossaryTerm("bead", "manik"),
             GlossaryTerm("Bead", "butiran")]  # terjemahan bentrok
    kinds = sorted(c.kind for c in detect_conflicts(terms))
    assert kinds == ["duplicate", "overlap", "translation_mismatch"]


def test_changed_terms_and_affected_units():
    old = [GlossaryTerm("Nobility", "kaum Nobility"), GlossaryTerm("bead", "manik")]
    new = [GlossaryTerm("Nobility", "kaum Nobility"),
           GlossaryTerm("bead", "manik-manik"),   # berubah
           GlossaryTerm("Hunter", "Pemburu")]      # tambah
    diff = changed_terms(old, new)
    assert diff["changed"] == ["bead"] and diff["added"] == ["Hunter"]
    assert diff["removed"] == []
    units = [_unit("u1", "The Hunter took the bead."),
             _unit("u2", "Nothing relevant here."),
             _unit("u3", "A bead of sweat.")]
    affected = units_affected_by_change(old, new, units)
    assert affected == ["u1", "u3"]


def test_validate_glossary():
    units = [_unit("u1", "The Hunter faced the Nobility."),
             _unit("u2", "A quiet evening.")]
    glossary = {"Hunter": "Pemburu", "Nobility": "kaum Nobility"}
    ok = {"u1": "Sang Pemburu menghadapi kaum Nobility.", "u2": "Malam yang tenang."}
    assert validate_glossary(units, ok, glossary) == []
    bad = {"u1": "Sang Hunter menghadapi kaum Bangsawan.", "u2": "Malam yang tenang."}
    viols = validate_glossary(units, bad, glossary)
    assert {v.term for v in viols} == {"Hunter", "Nobility"}
    assert all(v.unit_id == "u1" for v in viols)


def test_term_pattern_matches_pipeline_semantics():
    # word-boundary, case-insensitive, multi-kata
    assert term_pattern("Vampire Hunter").search("the Vampire Hunter D")
    assert term_pattern("Hunter").search("the Hunter asked")
    assert not term_pattern("Hunter").search("the Hunters asked")
    assert term_pattern("Noble").search("a Noble's blade")
    assert not term_pattern("Noble").search("the Nobility")


def test_export_glossary_file_compatible_with_cli(store, tmp_path):
    from novel_translator.models import parse_glossary
    mem = BookMemory(book_id="bm_x",
                     terms=[GlossaryTerm("Nobility", "kaum Nobility"),
                            GlossaryTerm("bead", "manik", approved=False)])
    store.save(mem)
    dest = store.export_glossary_file("bm_x", tmp_path / "g.txt")
    parsed = parse_glossary(dest.read_text(encoding="utf-8"))
    assert parsed == {"Nobility": "kaum Nobility"}  # hanya approved


def test_book_memory_glossary_identical_to_manual():
    """Inti anti-klaim-palsu: dict dari store == dict manual -> prompt identik."""
    mem = BookMemory(book_id="bm_x", terms=[
        GlossaryTerm("Nobility", "kaum Nobility"),
        GlossaryTerm("Vampire Hunter", "Pemburu Vampir"),
        GlossaryTerm("bead", "manik")])
    manual = {"Nobility": "kaum Nobility",
              "Vampire Hunter": "Pemburu Vampir",
              "bead": "manik"}
    assert mem.glossary_dict() == manual


def test_character_block_capped():
    mem = BookMemory(book_id="x", characters=[
        CharacterNote(name="D", relationships="x" * 5000)])
    block = mem.character_block(max_chars=200)
    assert len(block) <= 220
    assert "D" in block


def test_parse_known_from():
    from novel_translator.book_memory import parse_known_from
    assert parse_known_from("bab 3") == 3
    assert parse_known_from("Bab 2-5") == 2
    assert parse_known_from("5") == 5
    assert parse_known_from("") is None
    assert parse_known_from("sejak awal") is None


def test_notes_for_chapter_blocks_future_spoilers():
    from novel_translator.book_memory import notes_for_chapter
    notes = [CharacterNote(name="D", known_from="bab 1"),
             CharacterNote(name="Bos Rahasia", known_from="bab 9"),
             CharacterNote(name="Netral", known_from="")]
    ok, warns = notes_for_chapter(notes, 4)
    assert [n.name for n in ok] == ["D", "Netral"]
    ok, _ = notes_for_chapter(notes, 9)
    assert "Bos Rahasia" in [n.name for n in ok]
    ok, _ = notes_for_chapter(notes, None)
    assert len(ok) == 3


def test_character_block_chapter_filter_and_cap():
    mem = BookMemory(book_id="x", characters=[
        CharacterNote(name="D", known_from="bab 1", relationships="pemburu"),
        CharacterNote(name="Bos", known_from="bab 9", relationships="penjahat")])
    b4 = mem.character_block(chapter=4)
    assert "D" in b4 and "Bos" not in b4
    b9 = mem.character_block(chapter=9)
    assert "Bos" in b9


def test_fixture_epub_exists_and_parses(mini_epub):
    from novel_translator.epub import EpubBook
    units = EpubBook(str(mini_epub)).units
    assert len(units) >= 3
    assert compute_book_id(mini_epub) == compute_book_id(mini_epub)


def test_book_id_portable_no_absolute_paths(mini_epub):
    # fixture dibangun deterministik dari kode — tanpa path absolut lingkungan,
    # tanpa blob biner di git
    assert mini_epub.exists()


def test_changed_terms_approved_flip_counts_as_change():
    from novel_translator.book_memory import changed_terms
    old = [GlossaryTerm("Hunter", "Pemburu", approved=True)]
    new = [GlossaryTerm("Hunter", "Pemburu", approved=False)]
    diff = changed_terms(old, new)
    assert diff["changed"] == ["Hunter"]
    assert diff["added"] == [] and diff["removed"] == []
    # unit yang cocok ikut terdampak
    units = [_unit("u1", "The Hunter arrived."), _unit("u2", "Quiet night.")]
    assert units_affected_by_change(old, new, units) == ["u1"]


def test_validate_counts_occurrences():
    # kasus audit: "Hunter met Hunter" -> satu padanan hilang harus tertangkap
    u = _unit("u1", "Hunter met Hunter")
    gloss = {"Hunter": "Pemburu"}
    bad = {"u1": "Sang Pemburu bertemu Hunter"}
    viols = validate_glossary([u], bad, gloss)
    assert len(viols) == 1 and "2x" in viols[0].detail and "1x" in viols[0].detail
    good = {"u1": "Sang Pemburu bertemu Pemburu itu"}
    assert validate_glossary([u], good, gloss) == []


def test_validate_epub_pair_verified_and_unverifiable(tmp_path, mini_epub):
    from novel_translator.book_memory import validate_epub_pair
    import shutil
    import zipfile as zf
    # pasangan "terjemahan": salin fixture, ubah teks paragraf kedua
    out = tmp_path / "out.epub"
    shutil.copy(mini_epub, out)
    with zf.ZipFile(out, "a") as z:
        pass
    # bangun ulang dengan teks terjemahan (unitisasi sama)
    src_x = zf.ZipFile(mini_epub).read("OEBPS/c.xhtml").decode()
    tgt_x = src_x.replace("The Hunter met Hunter at dawn.",
                          "Sang Pemburu bertemu Hunter saat fajar.")
    tgt_x = tgt_x.replace("The old bead glowed faintly on the table.",
                          "Manik tua itu berpendar redup di atas meja.")
    with zf.ZipFile(mini_epub) as zin, zf.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "OEBPS/c.xhtml":
                data = tgt_x.encode()
            zout.writestr(item, data)
    res = validate_epub_pair(mini_epub, out, {"Hunter": "Pemburu", "bead": "manik"})
    assert res.status == "verified", res.detail
    assert res.units_checked >= 3
    assert any(v.term == "Hunter" for v in res.violations)
    assert not any(v.term == "bead" for v in res.violations)
    # rusak: hapus satu paragraf dari hasil -> unverifiable, bukan "konsisten"
    out2 = tmp_path / "out2.epub"
    tgt_x2 = tgt_x.replace("<p>Manik tua itu berpendar redup di atas meja.</p>", "")
    with zf.ZipFile(mini_epub) as zin, zf.ZipFile(out2, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "OEBPS/c.xhtml":
                data = tgt_x2.encode()
            zout.writestr(item, data)
    res2 = validate_epub_pair(mini_epub, out2, {"Hunter": "Pemburu"})
    assert res2.status == "unverifiable"
    assert res2.violations == []


def test_known_from_edge_cases():
    from novel_translator.book_memory import notes_for_chapter, parse_known_from
    assert parse_known_from("") is None
    assert parse_known_from("  ") is None
    assert parse_known_from("bab sepuluh") is None  # tak terurai
    notes = [CharacterNote(name="A", known_from=""),
             CharacterNote(name="B", known_from="ngawur"),
             CharacterNote(name="C", known_from="bab 3")]
    ok, warns = notes_for_chapter(notes, 2)
    assert [n.name for n in ok] == ["A", "B"]  # C ditahan
    assert any("B" in w for w in warns)  # peringatan untuk yang tak terurai
    ok_all, _ = notes_for_chapter(notes, None)
    assert len(ok_all) == 3


def test_validate_epub_pair_handles_output_xml_lang(tmp_path, mini_epub):
    # output pipeline bertanda xml:lang="id" — EpubBook menolak ekstrak;
    # validator harus menormalisasi salinan sementara, bukan gagal.
    from novel_translator.book_memory import validate_epub_pair
    import shutil
    import zipfile as zf
    out = tmp_path / "out_lang.epub"
    shutil.copy(mini_epub, out)
    src_x = zf.ZipFile(mini_epub).read("OEBPS/c.xhtml").decode()
    tgt_x = src_x.replace("<html ", '<html xml:lang="id" ', 1)
    tgt_x = tgt_x.replace("The Hunter met Hunter at dawn.",
                          "Sang Pemburu bertemu Pemburu saat fajar.")
    with zf.ZipFile(mini_epub) as zin, zf.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "OEBPS/c.xhtml":
                data = tgt_x.encode()
            zout.writestr(item, data)
    res = validate_epub_pair(mini_epub, out, {"Hunter": "Pemburu"})
    assert res.status == "verified", res.detail
    assert res.violations == []
