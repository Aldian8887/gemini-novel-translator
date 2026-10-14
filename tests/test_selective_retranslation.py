"""Langkah 3 v2.6.0: unit test selective re-translation (murni, tanpa I/O)."""
from types import SimpleNamespace

import pytest

from novel_translator.book_memory import (
    glossary_fingerprint,
    split_inherited_units,
)
from novel_translator.metrics import ProgressMetrics
from novel_translator.pipeline import _inheritance_source

pytestmark = pytest.mark.unit


def _unit(uid, text):
    run = SimpleNamespace(prefix="", text=text, suffix="", id=f"{uid}_r1")
    return SimpleNamespace(id=uid, runs=[run])


def test_split_partitions_correctly():
    units = [_unit("u1", "The Dark Lord rose."),
             _unit("u2", "A quiet village.")]
    old = {"Dark Lord": "Tuan Kegelapan"}
    new = {"Dark Lord": "Pangeran Kegelapan"}
    old_targets = {"u1_r1": "[ID] Tuan Kegelapan bangkit.",
                   "u2_r1": "[ID] Desa yang tenang."}
    inherited, affected = split_inherited_units(old, new, units, old_targets)
    assert inherited == {"u2_r1": "[ID] Desa yang tenang."}
    assert affected == {"u1_r1"}
    # partisi: warisan + terdampak = semua target lama
    assert set(inherited) | affected == set(old_targets)


def test_split_identical_glossary_inherits_all():
    units = [_unit("u1", "The Dark Lord rose.")]
    g = {"Dark Lord": "Tuan Kegelapan"}
    old_targets = {"u1_r1": "[ID] x"}
    inherited, affected = split_inherited_units(g, g, units, old_targets)
    assert inherited == old_targets
    assert affected == set()


def test_split_empty():
    assert split_inherited_units({}, {}, [], {}) == ({}, set())


def test_split_added_term_only_affects_matching_units():
    units = [_unit("u1", "The wand glowed."), _unit("u2", "The sword sang.")]
    old_targets = {"u1_r1": "a", "u2_r1": "b"}
    # tambah istilah "wand"; "sword" tidak berubah -> u2 tetap diwariskan
    inherited, affected = split_inherited_units(
        {"sword": "pedang"}, {"sword": "pedang", "wand": "tongkat"},
        units, old_targets)
    assert inherited == {"u2_r1": "b"}
    assert affected == {"u1_r1"}


def test_split_removed_term_marks_unit_affected():
    # Istilah yang DIHAPUS dari glossary: terjemahan lama dibuat di bawah
    # constraint itu, jadi unitnya wajib diterjemahkan ulang.
    units = [_unit("u1", "The sword sang.")]
    old_targets = {"u1_r1": "b"}
    inherited, affected = split_inherited_units(
        {"sword": "pedang"}, {}, units, old_targets)
    assert inherited == {}
    assert affected == {"u1_r1"}


def test_inheritance_source_guard():
    # namespace sama -> jalur load normal, bukan warisan
    assert _inheritance_source({"job_id": "a", "config_id": "c"}, "a", "c") is None
    # config berubah (model/style) -> isolasi, retranslate penuh
    assert _inheritance_source({"job_id": "a", "config_id": "c1"}, "b", "c2") is None
    # glossary berubah, config sama -> wariskan dari namespace lama
    assert _inheritance_source({"job_id": "a", "config_id": "c"}, "b", "c") == "a"
    # tanpa riwayat -> tidak ada warisan
    assert _inheritance_source(None, "b", "c") is None
    assert _inheritance_source({}, "b", "c") is None
    assert _inheritance_source({"config_id": "c"}, "b", "c") is None


def test_glossary_fingerprint_deterministic():
    assert (glossary_fingerprint({"b": "2", "a": "1"})
            == glossary_fingerprint({"a": "1", "b": "2"}))
    assert glossary_fingerprint({"a": "1"}) != glossary_fingerprint({"a": "2"})
    assert glossary_fingerprint({}) != glossary_fingerprint({"a": "1"})


def test_meter_inherit_no_request_inflation():
    runs = {"r1": SimpleNamespace(text="hello world"),
            "r2": SimpleNamespace(text="foo bar")}
    m = ProgressMetrics(runs, set())
    m.inherit(["r1"])
    assert m.done[0] == len("hello world")
    assert m.committed_requests == 0  # warisan = nol request API
    snap = m.snapshot.__self__  # noqa - hanya pastikan objek konsisten
    assert snap.done[0] == len("hello world")
