"""E2E Book Memory v1: chronological learning dengan fake translator + fake AI.

Menguji: urutan bab, glossary bertambah, pemakaian istilah antar-bab,
Auto Safe vs konflik, manual-menang, cancel/resume, tanpa duplikasi,
isolasi cache, EPUB valid.
"""
import threading
import time
import zipfile
from pathlib import Path

import pytest

from novel_translator.book_memory import BookMemoryStore, GlossaryTerm, compute_book_id
from novel_translator.models import TranslationOptions
from novel_translator.pipeline import translate_novel


def build_3chapter_epub(path: Path) -> Path:
    chapters = {
        "c1": ("Chapter One", ["Toto walked into Cronenberg.",
                               "The Frontier was quiet at dawn."]),
        "c2": ("Chapter Two", ["Toto met Wu-Lin near the Frontier.",
                               "A kraken coin bought their silence."]),
        "c3": ("Chapter Three", ["Wu-Lin spent the kraken coin in Cronenberg."]),
    }
    def doc(title, paras):
        ps = "\n".join(f"<p>{p}</p>" for p in paras)
        return ("<?xml version='1.0' encoding='utf-8'?>\n"
                "<html xmlns='http://www.w3.org/1999/xhtml'>\n"
                f"<head><title>{title}</title></head>\n<body>\n<h1>{title}</h1>\n{ps}\n"
                "</body>\n</html>")
    items = "".join(f"<item id='{k}' href='{k}.xhtml' media-type='application/xhtml+xml'/>"
                    for k in chapters)
    refs = "".join(f"<itemref idref='{k}'/>" for k in chapters)
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='u'>\n"
           "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<dc:title>Fixture v1</dc:title><dc:language>en</dc:language>"
           "<dc:identifier id='u'>fixture-v1</dc:identifier></metadata>\n"
           f"<manifest>{items}</manifest>\n<spine>{refs}</spine></package>")
    cont = ("<?xml version='1.0' encoding='utf-8'?>\n"
            "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>\n"
            "<rootfiles><rootfile full-path='OEBPS/c.opf' "
            "media-type='application/oebps-package+xml'/></rootfiles></container>")
    path = Path(path)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", cont, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/c.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        for k, (title, paras) in chapters.items():
            z.writestr(f"OEBPS/{k}.xhtml", doc(title, paras),
                       compress_type=zipfile.ZIP_DEFLATED)
    return path


class FakeTranslator:
    glossaries_seen: list = []

    def __init__(self, options, limiter, cancel_event=None, status=None):
        self.options = options
        FakeTranslator.glossaries_seen.append(dict(options.glossary))

    def translate_batch(self, batch):
        return {r.id: f"[ID] {r.text}" for u in batch for r in u.runs}

    def close(self):
        pass


# Discovery: bab 1 -> Toto, Cronenberg, Frontier (person/place, bersih).
# Bab 2 -> Wu-Lin (baru), Frontier->"Perbatasan" (KONFLIK dengan approved),
#          kraken coin (artifact -> pending).
_DISCOVERY = {
    1: [{"source_term": "Toto", "category": "person", "proposed_indonesian": "Toto",
         "supporting_text": "Toto walked into Cronenberg."},
        {"source_term": "Cronenberg", "category": "place", "proposed_indonesian": "Cronenberg",
         "supporting_text": "Toto walked into Cronenberg."},
        {"source_term": "Frontier", "category": "place", "proposed_indonesian": "Frontier",
         "supporting_text": "The Frontier was quiet at dawn."}],
    2: [{"source_term": "Wu-Lin", "category": "person", "proposed_indonesian": "Wu-Lin",
         "supporting_text": "Toto met Wu-Lin near the Frontier."},
        {"source_term": "Frontier", "category": "place", "proposed_indonesian": "Perbatasan",
         "supporting_text": "Toto met Wu-Lin near the Frontier."},
        {"source_term": "kraken coin", "category": "artifact", "proposed_indonesian": "koin kraken",
         "supporting_text": "A kraken coin bought their silence."}],
    3: [],
}


class FakeAI:
    def __init__(self, options, limiter, cancel_event=None, status=None):
        pass

    def discover(self, text, chapter_index):
        terms = [dict(t) for t in _DISCOVERY.get(chapter_index, [])]
        return terms, 100, 50

    def verify(self, cands, canonical, batch_size=5):
        out = []
        for c in cands:
            # adversarial: "Perbatasan" ditolak konsistensi
            if c["proposed_indonesian"] == "Perbatasan":
                out.append({"source_term": c["source_term"], "verdict": "ambiguous",
                            "reasoning": "conflict", "issues": ["consistency"],
                            "corrected_translation": None})
            else:
                out.append({"source_term": c["source_term"], "verdict": "verified",
                            "reasoning": "ok", "issues": [],
                            "corrected_translation": None})
        return out, 60, 30


def _options(**kw):
    base = dict(api_key="x", model="gemini-3.5-flash-lite", glossary={}, workers=2,
                auto_glossary=True, auto_verify=True, approval_policy="auto_safe",
                cache_dir=".test_cache_v1")
    base.update(kw)
    o = TranslationOptions(**base)
    o.validate()
    return o


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Isolasi Book Memory antar-test (jangan pakai ~/.novel_translator).
    import novel_translator.book_memory as _bm
    monkeypatch.setattr(_bm, "DEFAULT_ROOT", tmp_path / "book_memory")
    FakeTranslator.glossaries_seen = []
    src = build_3chapter_epub(tmp_path / "src.epub")
    return tmp_path, src


@pytest.mark.integration
def test_chronological_learning_e2e(workdir):
    tmp_path, src = workdir
    out = tmp_path / "out.epub"
    res = translate_novel(src, out, _options(), translator_factory=FakeTranslator,
                          glossary_ai_factory=FakeAI)
    assert res.complete, res.message
    assert out.exists()

    book_id = compute_book_id(src)
    mem = BookMemoryStore().load(book_id)
    # 1-2. glossary bertambah; progress kronologis
    assert mem.translation_progress == 3
    by_term = {t.term: t for t in mem.terms}
    assert by_term["Toto"].approved and by_term["Toto"].origin == "auto"
    assert by_term["Toto"].source_chapter == 1
    assert by_term["Wu-Lin"].source_chapter == 2
    # kraken coin: artifact -> pending review (tidak auto-approved)
    # status provisional: AI verified tapi kategori di luar Auto Safe
    assert not by_term["kraken coin"].approved
    assert by_term["kraken coin"].status == "provisional"
    # 4. konflik: "Perbatasan" TIDAK menimpa approved "Frontier"
    assert by_term["Frontier"].translation == "Frontier"
    assert by_term["Frontier"].approved
    # 3. translator bab 2 melihat istilah bab 1; bab 1 tidak melihat istilah bab 2
    g = FakeTranslator.glossaries_seen
    assert len(g) >= 3
    assert g[0] == {}, g[0]  # bab 1: belum ada discovery -> tanpa info masa depan
    assert "Toto" in g[1] and "Frontier" in g[1], g[1]  # bab 2: pakai hasil bab 1
    assert "Wu-Lin" not in g[1], g[1]  # ...tetapi bukan istilah bab 2 sendiri
    assert "Wu-Lin" in g[2] and "Toto" in g[2], g[2]      # bab 3: pakai bab 1+2
    assert "kraken coin" not in g[2], g[2]  # pending review tidak diinjeksikan
    # 8. tidak ada duplikasi
    names = [t.term for t in mem.terms]
    assert len(names) == len(set(names)), names


@pytest.mark.integration
def test_manual_glossary_wins_and_review_first(workdir):
    tmp_path, src = workdir
    out = tmp_path / "out2.epub"
    book_id = compute_book_id(src)
    store = BookMemoryStore()
    mem = store.load(book_id)
    mem.terms.append(GlossaryTerm(term="Frontier", translation="Frontier",
                                  approved=True, origin="manual"))
    store.save(mem)
    res = translate_novel(src, out, _options(approval_policy="review_first"),
                          translator_factory=FakeTranslator, glossary_ai_factory=FakeAI)
    assert res.complete
    mem = store.load(book_id)
    by_term = {t.term: t for t in mem.terms}
    # manual menang: tetap "Frontier"
    assert by_term["Frontier"].translation == "Frontier"
    # review_first: tidak ada auto-approved
    assert not [t for t in mem.terms if t.origin == "auto" and t.approved]


@pytest.mark.integration
def test_cancel_resume_no_duplicates(workdir):
    tmp_path, src = workdir
    out = tmp_path / "out3.epub"
    book_id = compute_book_id(src)
    cancel = threading.Event()

    orig_discover = FakeAI.discover
    def slow_discover(self, text, chapter_index):
        time.sleep(0.3)
        return orig_discover(self, text, chapter_index)
    FakeAI.discover = slow_discover
    try:
        t = threading.Thread(target=translate_novel,
                             kwargs=dict(input_path=src, output_base=out,
                                         options=_options(),
                                         translator_factory=FakeTranslator,
                                         glossary_ai_factory=FakeAI,
                                         cancel_event=cancel))
        t.start()
        # tunggu bab 1 terbit lalu batalkan
        store = BookMemoryStore()
        for _ in range(100):
            if store.load(book_id).translation_progress >= 1:
                break
            time.sleep(0.1)
        cancel.set()
        t.join(timeout=60)
        assert not t.is_alive()
        mem = store.load(book_id)
        assert mem.translation_progress >= 1
        n_terms_after_cancel = len(mem.terms)
        # resume tanpa cancel
        res = translate_novel(src, out, _options(), translator_factory=FakeTranslator,
                              glossary_ai_factory=FakeAI)
        assert res.complete
        mem = store.load(book_id)
        assert mem.translation_progress == 3
        names = [t.term for t in mem.terms]
        assert len(names) == len(set(names))
        # istilah bab 1 tidak diduplikasi saat resume
        assert len([t for t in mem.terms if t.term == "Toto"]) == 1
    finally:
        FakeAI.discover = orig_discover


@pytest.mark.integration
def test_legacy_path_unaffected(workdir):
    tmp_path, src = workdir
    out = tmp_path / "out4.epub"
    res = translate_novel(src, out, _options(auto_glossary=False, auto_verify=False),
                          translator_factory=FakeTranslator)
    assert res.complete
    # satu translator untuk seluruh buku (bukan per bab)
    assert len(FakeTranslator.glossaries_seen) == 1


@pytest.mark.integration
def test_selective_retranslation_only_affected_units(workdir):
    """Langkah 3 v2.6.0: ubah glossary manual -> hanya unit yang sumbernya
    mengandung istilah berubah yang diterjemahkan ulang; sisanya diwariskan
    dari namespace lama (guard config_id tetap mengizinkan karena hanya
    glossary yang berubah)."""
    tmp_path, src = workdir

    seen_batches = []  # [(unit_id, run_id, source_text)]

    class CountingTranslator(FakeTranslator):
        def translate_batch(self, batch):
            for u in batch:
                for r in u.runs:
                    seen_batches.append((u.id, r.id, r.text))
            return super().translate_batch(batch)

    out1 = tmp_path / "out_sel1.epub"
    res1 = translate_novel(src, out1, _options(),
                           translator_factory=CountingTranslator,
                           glossary_ai_factory=FakeAI)
    assert res1.complete, res1.message
    run1_units = {uid for uid, _, _ in seen_batches}
    assert run1_units, "run 1 harus menerjemahkan semua"

    # "Cronenberg" muncul di bab 1 & 3 -> ubah padanannya via glossary manual.
    seen_batches.clear()
    out2 = tmp_path / "out_sel2.epub"
    res2 = translate_novel(src, out2,
                           _options(glossary={"Cronenberg": "Kronenberg"}),
                           translator_factory=CountingTranslator,
                           glossary_ai_factory=FakeAI)
    assert res2.complete, res2.message
    assert seen_batches, "run 2 harus menerjemahkan ulang unit terdampak"
    run2_units = {uid for uid, _, _ in seen_batches}
    assert run2_units < run1_units, \
        f"harus parsial, bukan penuh: {len(run2_units)} vs {len(run1_units)} unit"
    # setiap unit yang diterjemahkan ulang harus mengandung "Cronenberg"
    # (granularity = unit: judul satu unit dengan paragraf ikut serta)
    by_unit = {}
    for uid, rid, text in seen_batches:
        by_unit.setdefault(uid, []).append(text)
    for uid, texts in by_unit.items():
        assert any("cronenberg" in t.casefold() for t in texts), uid
    assert any(uid not in run2_units for uid in run1_units), \
        "harus ada unit yang diwariskan tanpa API call"


@pytest.mark.integration
def test_selective_retranslation_blocked_on_model_change(workdir):
    """Guard config_id: ganti model (tanpa ubah glossary) -> retranslate penuh,
    tidak ada warisan antar-model."""
    tmp_path, src = workdir

    seen_batches = []

    class CountingTranslator(FakeTranslator):
        def translate_batch(self, batch):
            for u in batch:
                for r in u.runs:
                    seen_batches.append(r.id)
            return super().translate_batch(batch)

    out1 = tmp_path / "out_cfg1.epub"
    res1 = translate_novel(src, out1, _options(),
                           translator_factory=CountingTranslator,
                           glossary_ai_factory=FakeAI)
    assert res1.complete
    n1 = len(seen_batches)

    seen_batches.clear()
    out2 = tmp_path / "out_cfg2.epub"
    res2 = translate_novel(src, out2, _options(model="gemini-3.1-flash-lite"),
                           translator_factory=CountingTranslator,
                           glossary_ai_factory=FakeAI)
    assert res2.complete
    # model berbeda -> config_id berbeda -> warisan diblokir -> penuh lagi
    assert len(seen_batches) == n1, \
        f"harusnya retranslate penuh: {len(seen_batches)} vs {n1}"


@pytest.mark.integration
def test_character_notes_injected_per_chapter_antispoiler(workdir):
    """Langkah 4 v2.6.0: character_notes=True -> tiap bab disuntik blok catatan
    (anti-spoiler: known_from bab 2 tidak muncul di bab 1); OFF -> kosong."""
    from novel_translator.book_memory import CharacterNote
    tmp_path, src = workdir
    store = BookMemoryStore()
    mem = store.load(compute_book_id(src))
    mem.characters.append(CharacterNote(name="Toto", title="Tuan",
                                        known_from="bab 1"))
    mem.characters.append(CharacterNote(name="Wu-Lin", title="Nona",
                                        known_from="bab 2"))
    store.save(mem)

    from novel_translator import token_budget as _tb
    seen = []
    orig_init = _tb.PromptBuilder.__init__

    def spy(self, options, chapter_notes=""):
        seen.append(chapter_notes)
        orig_init(self, options, chapter_notes=chapter_notes)

    _tb.PromptBuilder.__init__ = spy
    try:
        out = tmp_path / "out_cn.epub"
        res = translate_novel(src, out,
                              _options(auto_glossary=True, character_notes=True),
                              translator_factory=FakeTranslator,
                              glossary_ai_factory=FakeAI)
    finally:
        _tb.PromptBuilder.__init__ = orig_init
    assert res.complete, res.message
    assert len(seen) >= 3, f"PromptBuilder dibangun per bab: {len(seen)}"
    assert all(seen), "setiap bab harus dapat blok catatan"
    assert any("Toto" in s for s in seen)
    # anti-spoiler: bab 1 (konstruksi pertama, kronologis) tak kenal Wu-Lin
    assert "Wu-Lin" not in seen[0], seen[0]
    assert any("Wu-Lin" in s for s in seen[1:])
