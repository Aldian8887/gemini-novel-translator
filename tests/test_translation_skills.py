"""Regression tests untuk Webnovel Translation Skill (Lean).

Kontrak yang dijaga:
1. Skill OFF (default): settings()/job identity byte-identik dengan perilaku
   sebelum skill ada; prompt instruction tidak berubah.
2. Skill ON: konfigurasi masuk identitas cache (isolasi antar profil dan
   antar versi skill); prompt deterministik.
3. Tidak ada API call; semua tes offline dengan mock.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novel_translator.models import TranslationOptions, canonical, digest
from novel_translator.translation_skills import (
    SKILL_VERSION, compose_skill_text, skill_overhead_tokens,
    skill_settings_fragment, validate_skill, PROFILE_CHOICES,
)
from novel_translator.token_budget import PromptBuilder, SYSTEM


def test_skill_off_tidak_menambah_key_settings():
    s = TranslationOptions().settings()
    assert not any(k.startswith("skill") for k in s)
    assert skill_settings_fragment("off", "general") == {}


def test_skill_on_masuk_settings():
    s = TranslationOptions(skill="lean", skill_profile="xianxia").settings()
    assert s["skill"] == "lean"
    assert s["skill_version"] == SKILL_VERSION
    assert s["skill_profile"] == "xianxia"


def test_isolasi_cache_antar_profil():
    d_off = digest(canonical(TranslationOptions().settings()))
    d_xian = digest(canonical(TranslationOptions(skill="lean", skill_profile="xianxia").settings()))
    d_myst = digest(canonical(TranslationOptions(skill="lean", skill_profile="mystery").settings()))
    assert d_off != d_xian
    assert d_xian != d_myst
    assert d_xian == digest(canonical(TranslationOptions(skill="lean", skill_profile="xianxia").settings()))


def test_komposisi_deterministik():
    t1 = compose_skill_text("lean", "xianxia")
    t2 = compose_skill_text("lean", "xianxia")
    assert t1 == t2 and len(t1) > 0
    assert compose_skill_text("off", "xianxia") == ""
    assert all(compose_skill_text("lean", p) for p in PROFILE_CHOICES)


def test_overhead_terukur():
    assert skill_overhead_tokens("off", "general") == 0
    ov = {p: skill_overhead_tokens("lean", p) for p in PROFILE_CHOICES}
    assert all(v < 400 for v in ov.values()), ov


@pytest.mark.parametrize("bad", [("turbo", "general"), ("lean", "noir"), ("off", "noir")])
def test_validate_menolak_nilai_asing(bad):
    with pytest.raises(ValueError):
        validate_skill(*bad)


def test_options_validate_menolak_skill_asing():
    with pytest.raises(ValueError):
        TranslationOptions(skill="turbo").validate()


def test_prompt_off_tanpa_skill_dan_identik_pra_skill():
    builder = PromptBuilder(TranslationOptions())
    inst = builder.body([])["systemInstruction"]["parts"][0]["text"]
    assert "WEBNOVEL SKILL" not in inst
    assert inst == SYSTEM + "\n" + canonical({"style": builder.options.style,
                                              "additional_instruction": ""})


def test_prompt_lean_memuat_skill_dan_deterministik():
    builder = PromptBuilder(TranslationOptions(skill="lean", skill_profile="progression"))
    inst = builder.body([])["systemInstruction"]["parts"][0]["text"]
    assert compose_skill_text("lean", "progression") in inst
    assert inst == builder.body([])["systemInstruction"]["parts"][0]["text"]


def test_kontrak_body_tidak_berubah():
    body = PromptBuilder(TranslationOptions(skill="lean", skill_profile="progression")).body([])
    assert set(body) == {"systemInstruction", "contents", "generationConfig"}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
