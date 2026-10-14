"""Langkah 1 v2.6.0: ketepatan + performa detect_conflicts pasca pre-compile.

- Ekuivalensi: hasil implementasi baru harus IDENTIK dengan referensi naive
  (implementasi lama dua-arah) pada input acak — optimasi murni, nol
  perubahan perilaku.
- Performa: ratusan istilah sintetis selesai dalam anggaran waktu wajar.
"""
import re
import string
import time

import pytest
from hypothesis import given, settings, strategies as st

from novel_translator.book_memory import (
    GlossaryTerm,
    _norm_term,
    detect_conflicts,
)

pytestmark = pytest.mark.unit

_words = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8)
_phrases = st.lists(_words, min_size=1, max_size=3).map(lambda ws: " ".join(ws))


@st.composite
def _terms(draw):
    n = draw(st.integers(min_value=1, max_value=12))
    return [GlossaryTerm(term=draw(_phrases), translation=draw(_words))
            for _ in range(n)]


def _naive_conflicts(terms):
    """Referensi: implementasi lama (regex inline, dua arah) — hanya untuk uji."""
    out = []
    seen = {}
    for t in terms:
        key = t.normalized()
        if not key:
            continue
        if key in seen:
            other = seen[key]
            if _norm_term(other.translation) != _norm_term(t.translation):
                out.append(("translation_mismatch", other.term, t.term))
            else:
                out.append(("duplicate", other.term, t.term))
        else:
            seen[key] = t
    keys = list(seen)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", b) or \
               re.search(r"(?<!\w)" + re.escape(b) + r"(?!\w)", a):
                out.append(("overlap", seen[a].term, seen[b].term))
    return out


def _sig(conflicts):
    return [(c.kind, c.terms[0], c.terms[1]) for c in conflicts]


@given(_terms())
@settings(max_examples=60)
def test_precompiled_identical_to_naive(terms):
    """Pre-compile + satu arah harus byte-identik hasilnya dengan versi lama."""
    assert _sig(detect_conflicts(terms)) == _naive_conflicts(terms)


def test_overlap_short_in_long_only():
    """Hanya istilah pendek di dalam panjang yang dilaporkan (satu arah cukup)."""
    terms = [GlossaryTerm("naga api", "fire dragon"),
             GlossaryTerm("naga", "dragon"),
             GlossaryTerm("pedang", "sword")]
    kinds = [(c.kind, tuple(c.terms)) for c in detect_conflicts(terms)]
    assert ("overlap", ("naga api", "naga")) in kinds
    assert len([k for k in kinds if k[0] == "overlap"]) == 1


@pytest.mark.slow
def test_perf_many_terms():
    """800 istilah: selesai cepat (pre-compile, bukan compile per pasangan)."""
    terms = [GlossaryTerm(f"istilah {i} tambahan", f"term {i}")
             for i in range(800)]
    terms += [GlossaryTerm("istilah", "term")]  # memicu banyak overlap
    start = time.perf_counter()
    conflicts = detect_conflicts(terms)
    elapsed = time.perf_counter() - start
    assert any(c.kind == "overlap" for c in conflicts)
    assert elapsed < 5.0, f"terlalu lambat: {elapsed:.2f}s"
