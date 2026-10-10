"""Additional adversarial tests for RC2 Book Memory v1.
Copy to project root and run: python -m pytest -q test_rc2_independent.py
These tests intentionally FAIL against RC2; no production data/API is touched.
"""
from pathlib import Path
import importlib.util
import pytest

from novel_translator.glossary_auto import validate_candidate, route_approval
import novel_translator.book_memory as bm
from novel_translator.pipeline import translate_novel


def test_auto_safe_requires_term_in_its_own_supporting_quote():
    # Both the chapter and the evidence quote exist, but the evidence quote
    # belongs to a different sentence and does NOT substantiate the term.
    chapter = 'Zorba made a plan. Later, John walked here.'
    c = {'source_term': 'Zorba', 'proposed_indonesian': 'Zorba',
         'category': 'person', 'supporting_text': 'John walked here.'}
    findings = validate_candidate(c, chapter, set(), {})
    decision = route_approval(c, findings,
                              {'verdict': 'verified', 'corrected_translation': None},
                              {}, set(), 'auto_safe')
    assert not decision.approved, 'Evidence quote does not actually contain the claimed term.'


def test_auto_discovery_cannot_approve_conflict_with_manual_options(tmp_path, monkeypatch):
    # An explicit manual translation in TranslationOptions must be considered
    # by the approval router, not just by the per-chapter translation prompt.
    monkeypatch.setattr(bm, 'DEFAULT_ROOT', tmp_path / 'book_memory')
    test_support = Path(__file__).parent / 'test_book_memory_v1_e2e.py'
    spec = importlib.util.spec_from_file_location('e2e_support_rc2', test_support)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = mod.build_3chapter_epub(tmp_path / 'src.epub')
    opts = mod._options(cache_dir=tmp_path / 'cache',
                        glossary={'Toto': 'Nama Manual'})
    result = translate_novel(source, tmp_path / 'out.epub', opts,
                             translator_factory=mod.FakeTranslator,
                             glossary_ai_factory=mod.FakeAI)
    assert result.complete
    memory = bm.BookMemoryStore().load(bm.compute_book_id(source))
    conflicts = [t for t in memory.terms
                 if t.term.casefold() == 'toto' and t.approved
                 and t.origin == 'auto' and t.translation != 'Nama Manual']
    assert not conflicts, 'Auto Safe stored an approved mapping conflicting with the user manual glossary.'
