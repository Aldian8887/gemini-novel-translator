"""Test Book Memory v1: discovery, validasi lokal, approval, kronologi."""
import pytest

from novel_translator.book_memory import (
    BookMemory, BookMemoryStore, GlossaryTerm,
)
from novel_translator import glossary_auto as ga


def _term(term, translation, **kw):
    base = dict(approved=True, status="verified", origin="manual")
    base.update(kw)
    return GlossaryTerm(term=term, translation=translation, **base)


# --- model memori ---

def test_glossary_upto_chapter_chronological():
    mem = BookMemory(book_id="b1", terms=[
        _term("Manual", "Manual", source_chapter=None),
        _term("Wu-Lin", "Wu-Lin", source_chapter=1, origin="auto"),
        _term("Frontier", "Frontier", source_chapter=2, origin="auto"),
    ])
    g1 = mem.glossary_upto_chapter(1)
    assert g1 == {"Manual": "Manual"}, g1  # bab 1: hanya manual
    g2 = mem.glossary_upto_chapter(2)
    assert g2 == {"Manual": "Manual", "Wu-Lin": "Wu-Lin"}, g2
    g3 = mem.glossary_upto_chapter(3)
    assert "Frontier" in g3


def test_pending_and_visible_terms_spoiler_safe():
    mem = BookMemory(book_id="b1", reader_progress=2, terms=[
        _term("A", "A", source_chapter=1),
        _term("B", "B", source_chapter=5),
        _term("C", "C", source_chapter=None),
        _term("D", "D", approved=False, status="provisional", origin="pending",
              source_chapter=1),
    ])
    visible = {t.term for t in mem.visible_terms()}
    assert visible == {"A", "C", "D"}, visible  # B (bab 5) disembunyikan
    pending = {t.term for t in mem.pending_terms()}
    assert pending == {"D"}, pending


def test_store_roundtrip_v1_fields(tmp_path):
    store = BookMemoryStore(tmp_path)
    mem = BookMemory(book_id="bm_x", title="T", translation_progress=3,
                     reader_progress=2, rejected_terms=["foo"],
                     terms=[_term("Wu-Lin", "Wu-Lin", source_chapter=1,
                                  origin="auto", evidence="quote",
                                  history=[{"at": "x"}])])
    mem.record_api_usage("discovery", 100, 1)
    store.save(mem)
    back = store.load("bm_x")
    assert back.translation_progress == 3
    assert back.reader_progress == 2
    assert back.rejected_terms == ["foo"]
    assert back.terms[0].origin == "auto"
    assert back.terms[0].evidence == "quote"
    assert back.api_usage["discovery"]["tokens"] == 100


# --- local validator ---

def test_local_validator_catches_typo_and_addition():
    text = "Vampire Hunter D walked in. Kraken coins were produced in limited quantities."
    seen, approved = set(), {}
    t1 = {"source_term": "Vampire Hunter D", "proposed_indonesian": "Pemburu Vamper D",
          "supporting_text": "Vampire Hunter D walked in."}
    f1 = ga.validate_candidate(t1, text, seen, approved)
    assert any(f.rule in ("R2", "R6") for f in f1), [f.rule for f in f1]
    t2 = {"source_term": "kraken coin", "proposed_indonesian": "koin kraken emas",
          "supporting_text": "Kraken coins were produced in limited quantities."}
    f2 = ga.validate_candidate(t2, text, seen, approved)
    assert any(f.rule == "R3" for f in f2)
    t3 = {"source_term": "D", "proposed_indonesian": "D",
          "supporting_text": "Vampire Hunter D walked in."}
    assert ga.validate_candidate(t3, text, seen, approved) == []


def test_local_validator_glossary_conflict():
    text = "They lived on the Frontier."
    t = {"source_term": "Frontier", "proposed_indonesian": "Perbatasan",
         "supporting_text": "They lived on the Frontier."}
    f = ga.validate_candidate(t, text, set(), {"Frontier": "Frontier"})
    assert any(f.rule == "R5" for f in f)


# --- approval router ---

def _cand(term, prop, category="person"):
    return {"source_term": term, "proposed_indonesian": prop, "category": category,
            "supporting_text": term}


def test_router_auto_safe_all_conditions():
    d = ga.route_approval(_cand("Toto", "Toto"), [],
                          {"verdict": "verified"}, {}, set(), "auto_safe")
    assert d.approved and d.status == "verified" and d.origin == "auto"


def test_router_blocks_on_local_findings():
    findings = [ga.LocalFinding("R3", "x")]
    d = ga.route_approval(_cand("kraken coin", "koin kraken emas", "artifact"),
                          findings, {"verdict": "verified"}, {}, set(), "auto_safe")
    assert not d.approved


def test_router_blocks_non_person_place():
    d = ga.route_approval(_cand("kraken coin", "koin kraken", "artifact"),
                          [], {"verdict": "verified"}, {}, set(), "auto_safe")
    assert not d.approved and d.status == "provisional"


def test_router_review_first_never_auto():
    d = ga.route_approval(_cand("Toto", "Toto"), [],
                          {"verdict": "verified"}, {}, set(), "review_first")
    assert not d.approved and d.origin == "pending"


def test_router_rejected_terms_stay_rejected():
    d = ga.route_approval(_cand("Foo", "Foo"), [],
                          {"verdict": "verified"}, {}, {"foo"}, "auto_safe")
    assert d.status == "rejected" and not d.approved


def test_router_manual_glossary_wins():
    findings = [ga.LocalFinding("R5", "konflik")]
    d = ga.route_approval(_cand("Frontier", "Perbatasan", "place"),
                          findings, {"verdict": "verified"},
                          {"Frontier": "Frontier"}, set(), "auto_safe")
    assert not d.approved and d.status == "ambiguous"


# --- chapter helpers ---

class _FakeDoc:
    pass


def test_group_chapters_and_job_id():
    class U:
        def __init__(self, doc):
            self.document = doc
    class B:
        documents = {"a.xhtml": 1, "b.xhtml": 2}
        units = [U("a.xhtml"), U("a.xhtml"), U("b.xhtml")]
    chs = ga.group_chapters(B())
    assert [c[0] for c in chs] == [1, 2]
    assert [len(c[1]) for c in chs] == [2, 1]
    j1, j2 = ga.chapter_job_id("base", 1), ga.chapter_job_id("base", 2)
    assert j1 != j2 and ga.chapter_job_id("base", 1) == j1
    # hash glossary: perubahan glossary -> namespace berbeda (tidak diam-diam reuse)
    g1 = ga.chapter_job_id("base", 1, {"Toto": "Toto"})
    g2 = ga.chapter_job_id("base", 1, {"Toto": "Nama Pengganti"})
    g3 = ga.chapter_job_id("base", 1, {"Toto": "Toto"})
    assert g1 != g2 and g1 == g3
    # tanpa glossary (None) tetap deterministik seperti sebelumnya
    assert ga.chapter_job_id("base", 1) == ga.chapter_job_id("base", 1)


def test_effective_chapter_glossary_manual_wins_case_insensitive():
    mem = BookMemory(book_id="t", terms=[
        GlossaryTerm("Toto", "Toto", origin="auto", source_chapter=1, approved=True),
        GlossaryTerm("Wu-Lin", "Wu-Lin", origin="auto", source_chapter=1, approved=True),
    ])
    eff = ga.effective_chapter_glossary(mem, 2, {"toto": "Nama Manual"})
    assert eff["toto"] == "Nama Manual"
    assert "Toto" not in eff  # tidak ada duplikat case-berbeda
    assert eff["Wu-Lin"] == "Wu-Lin"


def test_r8_rejects_term_absent_from_source():
    # Zorba tidak muncul di teks bab -> R8, tidak boleh Auto Safe
    cand = {"source_term": "Zorba", "proposed_indonesian": "Zorba",
            "category": "person", "supporting_text": "John walked here."}
    f = ga.validate_candidate(cand, "John walked here.", set(), {})
    assert "R8" in [x.rule for x in f]
    d = ga.route_approval(cand, f, {"verdict": "verified", "corrected_translation": None},
                          {}, set(), "auto_safe")
    assert not d.approved
    # Istilah yang ADA di sumber tidak kena R8
    cand2 = {"source_term": "John", "proposed_indonesian": "John",
             "category": "person", "supporting_text": "John walked here."}
    f2 = ga.validate_candidate(cand2, "John walked here.", set(), {})
    assert "R8" not in [x.rule for x in f2]


def test_spoiler_fail_closed_by_default():
    mem = BookMemory(book_id="t", terms=[
        GlossaryTerm("A", "A", source_chapter=1, approved=True),
        GlossaryTerm("B", "B", source_chapter=5, approved=True),
    ])
    # Belum diatur -> hanya bab 1 (fail-closed)
    assert [t.term for t in mem.visible_terms()] == ["A"]
    # Eksplisit tampil semua
    mem.show_all_terms = True
    assert len(mem.visible_terms()) == 2
    # Reader progress tetap dihormati
    mem.show_all_terms = False
    mem.reader_progress = 5
    assert len(mem.visible_terms()) == 2


def test_spoiler_safe_export_respects_reader_progress():
    # reader_progress=2, istilah dari bab 5 tidak boleh ikut ekspor aman.
    from novel_translator.book_memory import spoiler_safe_glossary_text
    mem = BookMemory(book_id="t", terms=[
        GlossaryTerm("Toto", "Toto", source_chapter=1, approved=True),
        GlossaryTerm("secret-revelation", "revelasi rahasia", source_chapter=5,
                     approved=True),
        GlossaryTerm("Draft", "draf", source_chapter=5, approved=False),
    ])
    mem.reader_progress = 2
    safe = spoiler_safe_glossary_text(mem)
    assert "Toto = Toto" in safe
    assert "secret-revelation" not in safe  # bab 5 disembunyikan
    assert "Draft" not in safe  # belum approved
    # show_all_terms eksplisit -> semua approved ikut
    mem.show_all_terms = True
    full_safe = spoiler_safe_glossary_text(mem)
    assert "secret-revelation = revelasi rahasia" in full_safe
    assert "Toto = Toto" in full_safe


# --- run_chapter_discovery (idempoten, tanpa API) ---

class _FakeAI:
    def __init__(self, terms):
        self._terms = terms
        self.tokens_used = 0
        self.requests_made = 0

    def discover(self, text, chapter_index):
        self.requests_made += 1
        self.tokens_used += 100
        return self._terms, 80, 20

    def verify(self, cands, canonical, batch_size=5):
        self.requests_made += 1
        self.tokens_used += 50
        return [{"source_term": c["source_term"], "verdict": "verified",
                 "reasoning": "ok", "issues": [],
                 "corrected_translation": None} for c in cands], 40, 10


class _FakeRun:
    def __init__(self, text):
        self.prefix, self.text, self.suffix = "", text, ""


class _FakeUnit:
    def __init__(self, text):
        self.runs = [_FakeRun(text)]


def test_run_chapter_discovery_idempotent(tmp_path):
    store = BookMemoryStore(tmp_path)
    mem = BookMemory(book_id="bm_y")
    terms = [{"source_term": "Toto", "category": "person",
              "proposed_indonesian": "Toto",
              "supporting_text": "Toto walked in."}]
    units = [_FakeUnit("Toto walked in.")]
    fake = _FakeAI(terms)
    r1 = ga.run_chapter_discovery(1, units, mem, store, fake, "auto_safe", True)
    assert r1.discovered == 1 and r1.auto_approved == 1
    assert mem.translation_progress == 0  # discovery tidak mengubah progress translasi
    assert mem.discovery_log["1"]["status"] == "published"
    # idempoten: panggil lagi -> tidak ada duplikat, tidak ada request baru
    n_req = fake.requests_made
    r2 = ga.run_chapter_discovery(1, units, mem, store, fake, "auto_safe", True)
    assert r2.discovered == 0 and fake.requests_made == n_req
    assert len(mem.terms) == 1
    # reload dari disk: tetap konsisten
    back = store.load("bm_y")
    assert len(back.terms) == 1 and back.terms[0].approved
