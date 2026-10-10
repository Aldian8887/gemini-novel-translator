"""Webnovel Translation Skill — paket instruksi sastra opsional dan berversi.

Prinsip integrasi (diwarisi dari audit arsitektur):
- Skill OFF (default): perilaku byte-identik dengan sebelumnya; tidak ada
  overhead prompt dan tidak ada perubahan identitas cache.
- Skill ON: teks instruksi ringkas digabung ke system instruction oleh
  PromptBuilder; konfigurasi skill masuk identitas cache HANYA saat aktif,
  dengan versioning terpisah (SKILL_VERSION) sehingga PROMPT_VERSION dan
  ENGINE_VERSION tidak perlu berubah.
- Tidak ada API call tambahan, tidak ada dependensi baru. Modul ini hanya
  memakai stdlib agar tidak ada import cycle dengan token_budget/models.
"""

from __future__ import annotations

SKILL_VERSION = "1.0"

SKILL_OFF = "off"
SKILL_LEAN = "lean"
SKILL_LEVELS = (SKILL_OFF, SKILL_LEAN)

# Inti ringkas (dipakai semua profil): padat, tanpa basa-basi.
_SKILL_CORE = (
    "WEBNOVEL SKILL: Translate as a professional English-to-Indonesian literary "
    "translator for serialized webnovels. Priority order: (1) semantic fidelity — "
    "translate meaning, not English sentence structure; preserve causality, negation, "
    "uncertainty and temporal order; never resolve ambiguity the author left open; "
    "never add explanations or implications absent from the source. "
    "(2) Terminology consistency — glossary entries are authoritative; keep names, "
    "titles, abilities and ranks identical across chapters; never invent translations "
    "for established terms. (3) Character voice — keep each speaker's register; "
    "choose Indonesian pronouns and forms of address from context and hierarchy; do "
    "not force aku/kau everywhere; do not invent intimacy or hostility. "
    "(4) Atmosphere — match the passage's register; do not overdramatize neutral "
    "narration or exaggerate descriptions; add no metaphors."
)

# Profil genre: satu blok ringkas per genre. Kunci stabil untuk identitas cache.
SKILL_PROFILES: dict[str, tuple[str, str]] = {
    "general": (
        "Umum (webnovel)",
        "GENRE: general webnovel. Natural Indonesian narration, faithful meaning, "
        "context-sensitive dialogue. Avoid excessive literary embellishment.",
    ),
    "xianxia": (
        "Xianxia / Kultivasi",
        "GENRE: xianxia/cultivation (Chinese fantasy via English). Preserve "
        "cultivation realms, sect hierarchy, honorifics, martial techniques, Daoist "
        "terms, artifact grades and power systems exactly; never retranslate ranks "
        "inconsistently. Do not translate proper names or established terms. Use "
        "culturally fitting Indonesian without inventing new cultural details.",
    ),
    "progression": (
        "Progression Fantasy / System",
        "GENRE: progression fantasy / system. Preserve ability names, skill "
        "classifications, stats, levels, ranks, item names and system notifications "
        "with verbatim consistency. Keep every numeric value and rank relationship "
        "exact.",
    ),
    "mystery": (
        "Misteri / Cosmic Horror",
        "GENRE: mystery / cosmic horror. Preserve ambiguity, tension, psychological "
        "nuance and deliberate uncertainty. Never reveal implications earlier than "
        "the source. Do not exaggerate horror. Keep narrator knowledge separate "
        "from character knowledge.",
    ),
    "character": (
        "Character-Driven",
        "GENRE: character-driven fiction. Preserve each character's distinct speech "
        "pattern, politeness level, social hierarchy, relationship dynamics, "
        "emotional subtext and pronoun consistency. Do not make all characters "
        "sound alike; preserve deliberate shifts in behavior.",
    ),
}

PROFILE_CHOICES = tuple(SKILL_PROFILES)


def validate_skill(skill: str, profile: str) -> None:
    """Validasi nilai konfigurasi; raise ValueError bila tidak dikenal."""
    if skill not in SKILL_LEVELS:
        raise ValueError(f"Skill tidak dikenal: {skill!r}. Pilihan: {', '.join(SKILL_LEVELS)}.")
    if profile not in SKILL_PROFILES:
        raise ValueError(f"Profil skill tidak dikenal: {profile!r}.")


def compose_skill_text(skill: str, profile: str) -> str:
    """Susun teks instruksi skill; string kosong bila skill OFF.

    Deterministik: input sama selalu menghasilkan teks sama (syarat identitas
    cache dan reproduksibilitas prompt).
    """
    if skill == SKILL_OFF:
        return ""
    validate_skill(skill, profile)
    _label, body = SKILL_PROFILES[profile]
    return _SKILL_CORE + " " + body


def skill_overhead_tokens(skill: str, profile: str) -> int:
    """Estimasi overhead token per request dari teks skill (0 bila OFF)."""
    text = compose_skill_text(skill, profile)
    if not text:
        return 0
    from .token_budget import text_tokens  # lazy: hindari import cycle
    return text_tokens(text)


def skill_settings_fragment(skill: str, profile: str) -> dict:
    """Fragmen identitas cache untuk settings(); kosong bila skill OFF.

    Mengikuti pola domain/provider: nilai default sengaja tidak ditulis agar
    settings/job_id lama byte-identik dan cache lama tetap terbaca.
    """
    if skill == SKILL_OFF:
        return {}
    validate_skill(skill, profile)
    return {"skill": skill, "skill_profile": profile, "skill_version": SKILL_VERSION}
