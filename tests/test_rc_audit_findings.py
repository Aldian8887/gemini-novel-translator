"""Independent regression cases for Book Memory v1 RC (2026-10-10).
Run from project root: python -m pytest -q /path/to/test_rc_audit_findings.py
Failures identify current RC defects; this file does not edit project code.
Requires tests/test_book_memory_v1_e2e.py in project root.
"""
from pathlib import Path
import importlib.util
import pytest
from novel_translator.pipeline import translate_novel
from novel_translator.book_memory import BookMemoryStore, compute_book_id, GlossaryTerm
import novel_translator.book_memory as bm
from novel_translator.glossary_auto import validate_candidate, route_approval

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, 'DEFAULT_ROOT', tmp_path / 'book_mem')
    monkeypatch.chdir(Path(__file__).parent)  # Adapt if tests are copied elsewhere.
    tests_file = Path('test_book_memory_v1_e2e.py').resolve()
    spec = importlib.util.spec_from_file_location('rc_support', tests_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    src = module.build_3chapter_epub(tmp_path / 'src.epub')
    def options(**changes):
        return module._options(cache_dir=tmp_path / 'cache', **changes)
    return tmp_path, src, module, options

def first_run(sandbox):
    folder, src, mod, options = sandbox
    result = translate_novel(src, folder / 'first.epub', options(),
                             translator_factory=mod.FakeTranslator,
                             glossary_ai_factory=mod.FakeAI)
    assert result.complete
    return result

def test_changing_style_after_completed_run_still_translates(sandbox):
    first_run(sandbox)
    folder, src, mod, options = sandbox
    new = translate_novel(src, folder / 'changed_style.epub',
                          options(style='Another style'),
                          translator_factory=mod.FakeTranslator,
                          glossary_ai_factory=mod.FakeAI)
    assert new.complete

def test_ignore_cache_retranslates_completed_book(sandbox):
    first_run(sandbox)
    folder, src, mod, options = sandbox
    result = translate_novel(src, folder / 'fresh.epub',
                             options(ignore_cache=True),
                             translator_factory=mod.FakeTranslator,
                             glossary_ai_factory=mod.FakeAI)
    assert result.complete

def test_manual_glossary_overrides_auto_memory():
    book = bm.BookMemory(book_id='test', terms=[
        GlossaryTerm('Toto', 'Toto', origin='auto', source_chapter=1, approved=True)])
    manual = {'Toto': 'Nama Manual'}
    # Merge yang benar: memory dulu, manual menimpa (termasuk normalisasi kapital)
    from novel_translator.glossary_auto import effective_chapter_glossary
    effective = effective_chapter_glossary(book, 2, manual)
    assert effective['Toto'] == 'Nama Manual'
    # juga dengan kapitalisasi berbeda
    effective2 = effective_chapter_glossary(book, 2, {'toto': 'Nama Manual'})
    assert effective2['toto'] == 'Nama Manual'
    assert 'Toto' not in effective2

def test_auto_safe_rejects_term_absent_from_source():
    candidate = {'source_term':'Zorba','proposed_indonesian':'Zorba',
                 'category':'person','supporting_text':'John walked here.'}
    source = 'John walked here.'
    findings = validate_candidate(candidate,source,set(),{})
    decision = route_approval(candidate,findings,
                              {'verdict':'verified','corrected_translation':None},
                              {},set(),'auto_safe')
    assert not decision.approved

def test_glossary_change_cannot_silently_reuse_stale_chapter_cache(sandbox):
    first_run(sandbox)
    folder, src, mod, options = sandbox
    store = BookMemoryStore()
    memory = store.load(compute_book_id(src))
    for term in memory.terms:
        if term.term == 'Toto':
            term.translation = 'Nama Pengganti'
    store.save(memory)
    mod.FakeTranslator.glossaries_seen = []
    result = translate_novel(src, folder / 'after_edit.epub', options(),
                             translator_factory=mod.FakeTranslator,
                             glossary_ai_factory=mod.FakeAI)
    # Either invalidate affected cache/translate or explicitly refuse to reuse
    # incompatible old outputs. RC silently succeeds with zero translation calls.
    assert not result.complete or bool(mod.FakeTranslator.glossaries_seen)
