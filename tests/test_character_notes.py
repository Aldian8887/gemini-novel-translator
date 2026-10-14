"""Langkah 4 v2.6.0: character_notes strict opt-in (default OFF)."""
import pytest

from novel_translator.models import TranslationOptions
from novel_translator.token_budget import PromptBuilder

pytestmark = pytest.mark.unit


def _opts(**kw):
    base = dict(api_key="x", model="gemini-3.5-flash-lite")
    base.update(kw)
    o = TranslationOptions(**base)
    return o


def test_off_settings_byte_identical():
    """Default OFF (eksplisit False) -> settings identik dengan tanpa opsi."""
    a = _opts()
    b = _opts(character_notes=False)
    assert a.settings() == b.settings()
    assert "character_notes" not in a.settings()


def test_on_settings_records_flag():
    o = _opts(auto_glossary=True, character_notes=True)
    o.validate()
    assert o.settings()["character_notes"] is True


def test_requires_auto_glossary():
    o = _opts(character_notes=True)
    with pytest.raises(ValueError, match="auto_glossary"):
        o.validate()


def test_prompt_builder_byte_identical_when_off():
    o = _opts()
    b1 = PromptBuilder(o)
    b2 = PromptBuilder(o, chapter_notes="")
    assert b1.body([]) == b2.body([])


def test_prompt_builder_appends_notes_when_on():
    o = _opts()
    b = PromptBuilder(o, chapter_notes="Catatan karakter:\n- Toto")
    instr = b.body([])["systemInstruction"]["parts"][0]["text"]
    assert "Catatan karakter" in instr
    assert "Toto" in instr
    # dan benar-benar berbeda dari versi OFF
    assert b.body([]) != PromptBuilder(o).body([])
