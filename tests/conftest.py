"""Shared fixtures for the translator test suite (Batch D, 12 Okt 2026).

- sys.path bootstrap dipusatkan di sini (dulu diulang di tiap file test).
- Builder umum untuk Unit/Run/GlossaryTerm/CharacterNote.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from novel_translator.book_memory import CharacterNote, GlossaryTerm
from novel_translator.models import Run, Unit


@pytest.fixture
def make_run():
    def _make(rid="r1", text="Hello world", **kw):
        return Run(id=rid, text=text, **kw)
    return _make


@pytest.fixture
def make_unit(make_run):
    def _make(uid="u1", texts=("Hello world",), document="doc", context="ctx"):
        runs = tuple(make_run(rid=f"r{i}", text=t) for i, t in enumerate(texts))
        return Unit(id=uid, document=document, context=context, runs=runs)
    return _make


@pytest.fixture
def make_term():
    def _make(term="Nobility", translation="kaum bangsawan", **kw):
        return GlossaryTerm(term=term, translation=translation, **kw)
    return _make


@pytest.fixture
def make_note():
    def _make(name="Fang Yuan", known_from="", **kw):
        return CharacterNote(name=name, known_from=known_from, **kw)
    return _make
