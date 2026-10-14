"""Property-based tests for glossary invariants (Batch D, 12 Okt 2026).

Skill: python-testing-patterns (Pattern 10: hypothesis).
Menarget invarian yang dulu sempat jadi bug nyata (dedup span overlap,
enforce known_from — Book Memory v0, 10 Okt 2026): properti ini menjamin
regresi semacam itu tertangkap otomatis, bukan cuma lewat contoh tetap.
"""
import re
import string

import pytest
from hypothesis import given, settings, strategies as st

from novel_translator.book_memory import (
    CharacterNote,
    GlossaryTerm,
    detect_conflicts,
    notes_for_chapter,
    parse_known_from,
)

pytestmark = pytest.mark.unit

_words = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10)
_known_from = st.one_of(
    st.just(""),
    st.integers(min_value=1, max_value=60).map(lambda n: f"bab {n}"),
    st.integers(min_value=1, max_value=60).map(lambda n: f"bab {n}-{n+3}"),
    st.integers(min_value=1, max_value=60).map(str),
    st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=12),
)


@st.composite
def _notes(draw):
    return CharacterNote(name=draw(_words), known_from=draw(_known_from))


@st.composite
def _terms(draw):
    # _norm_term: strip + split + casefold; pastikan tak-kosong setelah norm.
    term = " ".join(draw(st.lists(_words, min_size=1, max_size=3)))
    return GlossaryTerm(term=term, translation=draw(_words))


# ------------------------------------------------------- parse_known_from
@given(st.text())
def test_parse_known_from_never_raises(s):
    result = parse_known_from(s)
    assert result is None or isinstance(result, int)


@given(st.text())
def test_parse_known_from_returns_first_int(s):
    m = re.search(r"(\d+)", s.strip().casefold())
    assert parse_known_from(s) == (int(m.group(1)) if m else None)


@given(st.integers(min_value=0, max_value=9999))
def test_parse_known_from_plain_number(n):
    assert parse_known_from(str(n)) == n
    assert parse_known_from(f"bab {n}") == n


# ------------------------------------------------------- notes_for_chapter
@given(st.lists(_notes(), max_size=15),
       st.one_of(st.none(), st.integers(min_value=1, max_value=60)))
def test_notes_for_chapter_no_future_spoiler(notes, chapter):
    """INVARIAN ANTI-SPOILER: catatan dari bab depan tak boleh lolos."""
    allowed, _warnings = notes_for_chapter(notes, chapter)
    for n in allowed:
        start = parse_known_from(n.known_from)
        assert start is None or chapter is None or chapter >= start, (
            f"'{n.name}' known_from='{n.known_from}' lolos untuk bab {chapter}")


@given(st.lists(_notes(), max_size=15))
def test_notes_for_chapter_none_passes_all(notes):
    allowed, _ = notes_for_chapter(notes, None)
    assert len(allowed) == len(notes)


# ------------------------------------------------------- detect_conflicts
@given(st.lists(_terms(), min_size=1, max_size=10))
def test_detect_conflicts_deterministic(terms):
    a = [(c.kind, tuple(c.terms)) for c in detect_conflicts(terms)]
    b = [(c.kind, tuple(c.terms)) for c in detect_conflicts(terms)]
    assert a == b


@given(st.lists(_terms(), min_size=1, max_size=8))
def test_duplicate_term_always_reported(terms):
    """Duplikat persis -> konflik 'duplicate' (atau mismatch bila beda arti)."""
    terms = terms + [GlossaryTerm(term=terms[0].term,
                                  translation=terms[0].translation)]
    kinds = {c.kind for c in detect_conflicts(terms)}
    assert kinds & {"duplicate", "translation_mismatch"}


@given(_words, _words)
def test_translation_mismatch_reported(a, b):
    terms = [GlossaryTerm(term="Nobility", translation=a),
             GlossaryTerm(term="nobility ", translation=b)]
    kinds = {c.kind for c in detect_conflicts(terms)}
    if a.strip().casefold() != b.strip().casefold():
        assert "translation_mismatch" in kinds
    else:
        assert "duplicate" in kinds


@given(_words, _words)
def test_overlap_reported(a, b):
    """'magic sword' mengandung 'sword' sebagai frasa utuh -> overlap."""
    terms = [GlossaryTerm(term=a, translation="x"),
             GlossaryTerm(term=f"{b} {a}".strip(), translation="y")]
    long, short = (terms[1], terms[0]) if b else (terms[0], terms[0])
    conflicts = detect_conflicts(terms)
    if b and re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", f"{b} {a}"):
        assert any(c.kind == "overlap" for c in conflicts)


@given(st.lists(_terms(), max_size=12))
@settings(max_examples=60)
def test_detect_conflicts_never_raises(terms):
    detect_conflicts(terms)  # regex dari input acak tak boleh meledak
