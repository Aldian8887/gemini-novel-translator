from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import InvalidResponse

# Cache identity, NOT the application release. Keep the v2 extraction contract.
ENGINE_VERSION = "2.0.0"
PROMPT_VERSION = 2
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# v2.4.0: penyedia kedua (gateway OpenAI-compatible), opsional dan opt-in.
# Default tetap "gemini" agar jalur lama tidak berubah sama sekali.
PROVIDERS = ("gemini", "openai")
DEFAULT_OA_BASE_URL = "https://api.bansosai.app/v1"
DEFAULT_OA_MODEL = "claude-sonnet-4-5"
DEFAULT_STYLE = "Natural dan mengalir seperti novel Indonesia modern"
NONFICTION_DEFAULT_STYLE = "Akurat, jernih, dan lugas seperti buku nonfiksi Indonesia modern"
DOMAINS = ("fiction", "nonfiction")
OFFICIAL_RPM, OFFICIAL_TPM, OFFICIAL_RPD = 15, 250000, 500

PROFILES = {
    "Safe": dict(rpm=10, tpm=230000, target_tpm=210000, rpd=470,
                 target_input_tokens=14000, max_input_tokens=18000, workers=1),
    "Balanced": dict(rpm=13, tpm=240000, target_tpm=225000, rpd=490,
                     target_input_tokens=24000, max_input_tokens=30000, workers=2),
    "Aggressive": dict(rpm=13, tpm=248000, target_tpm=240000, rpd=490,
                       target_input_tokens=30000, max_input_tokens=36000, workers=2),
}
RPD_PRESETS = {"Safe": 470, "Aggressive": 490, "Maximum": 498}


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


@dataclass(frozen=True)
class Run:
    id: str
    text: str
    style: str = ""
    prefix: str = ""
    suffix: str = ""

    def payload(self):
        return {"id": self.id, "text": self.text, "format": self.style,
                "space_before": bool(self.prefix), "space_after": bool(self.suffix)}


@dataclass(frozen=True)
class Unit:
    id: str
    document: str
    context: str
    runs: tuple[Run, ...]
    block_key: str = ""  # Packing-only boundary; excluded from the v2 payload/fingerprint.

    def subset(self, runs):
        return replace(self, runs=tuple(runs))

    def payload(self):
        return {"id": self.id, "context": self.context,
                "runs": [r.payload() for r in self.runs]}


def validate_text(run: Run, text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise InvalidResponse("Gemini mengembalikan bagian teks kosong.", same_batch_retry=True)
    # Only XML 1.0 characters are allowed, including astral Unicode.
    if any(not (ord(c) in (9, 10, 13) or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in text):
        raise InvalidResponse("Respons mengandung karakter yang tidak sah untuk XML.")
    text = text.strip()
    if len(text) > max(300, len(run.text) * 8):
        raise InvalidResponse("Panjang respons tidak wajar.")
    if len(run.text) > 160 and len(text) < len(run.text) * 0.25:
        raise InvalidResponse("Respons terlalu pendek; kemungkinan terpotong atau diringkas.")
    return text


def validate_response(units: list[Unit], parsed) -> dict[str, str]:
    expected_units = {u.id: u for u in units}
    if not isinstance(parsed, list) or len(parsed) != len(units):
        raise InvalidResponse("Jumlah unit respons tidak cocok.", same_batch_retry=True)
    result, seen_units = {}, set()
    for item in parsed:
        if not isinstance(item, dict) or set(item) != {"id", "runs"}:
            raise InvalidResponse("Skema unit respons tidak cocok.", same_batch_retry=True)
        uid = item["id"]
        if not isinstance(uid, str) or uid not in expected_units or uid in seen_units:
            raise InvalidResponse("ID unit hilang, duplikat, atau tidak dikenal.", same_batch_retry=True)
        seen_units.add(uid)
        expected = {r.id: r for r in expected_units[uid].runs}
        rows = item["runs"]
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise InvalidResponse("Jumlah potongan inline tidak cocok.", same_batch_retry=True)
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "text"}:
                raise InvalidResponse("Skema teks respons tidak cocok.", same_batch_retry=True)
            rid = row["id"]
            if not isinstance(rid, str) or rid not in expected or rid in seen:
                raise InvalidResponse("ID teks hilang, duplikat, atau tidak dikenal.", same_batch_retry=True)
            seen.add(rid)
            result[rid] = validate_text(expected[rid], row["text"])
    return result


def validate_response_partial(units: list[Unit], parsed) -> tuple[dict[str, str], set[str], list[str]]:
    """Salvage per potongan (v2.2.4): pisahkan run valid dari run rusak.

    Mengembalikan (terjemahan_valid, id_run_rusak, daftar_alasan). Unit yang
    hilang/duplikat menyumbang seluruh run-nya sebagai rusak; run lain dinilai
    sendiri-sendiri, sehingga satu ID rusak tidak lagi membuang batch utuh.
    Run yang diterima tetap lolos validate_text yang sama seperti jalur ketat.
    """
    good: dict[str, str] = {}
    bad: set[str] = set()
    issues: list[str] = []

    def note(reason: str) -> None:
        if reason and reason not in issues:
            issues.append(reason)

    def mark_unit_bad(unit: Unit, reason: str) -> None:
        note(reason)
        bad.update(r.id for r in unit.runs)

    if not isinstance(parsed, list):
        for unit in units:
            mark_unit_bad(unit, "Respons bukan daftar unit.")
        return good, bad, issues
    items: dict[str, dict] = {}
    duplicated: set[str] = set()
    expected_ids = {u.id for u in units}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        uid = item.get("id")
        if not isinstance(uid, str) or uid not in expected_ids:
            continue
        if uid in items:
            duplicated.add(uid)
        else:
            items[uid] = item
    for unit in units:
        if unit.id in duplicated:
            mark_unit_bad(unit, "ID unit duplikat dalam respons.")
            continue
        item = items.get(unit.id)
        if item is None:
            mark_unit_bad(unit, "Unit hilang dari respons.")
            continue
        rows = item.get("runs")
        if not isinstance(rows, list):
            mark_unit_bad(unit, "Daftar potongan inline unit tidak valid.")
            continue
        rowmap: dict[str, dict] = {}
        dup_runs: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            if not isinstance(rid, str):
                continue
            if rid in rowmap:
                dup_runs.add(rid)
            else:
                rowmap[rid] = row
        for run in unit.runs:
            if run.id in dup_runs:
                bad.add(run.id)
                note("ID teks duplikat dalam respons.")
                continue
            row = rowmap.get(run.id)
            if row is None:
                bad.add(run.id)
                note("ID teks hilang dari respons.")
                continue
            text = row.get("text")
            if not isinstance(text, str):
                bad.add(run.id)
                note("Skema teks respons tidak cocok.")
                continue
            try:
                good[run.id] = validate_text(run, text)
            except InvalidResponse as exc:
                bad.add(run.id)
                note(str(exc))
    return good, bad, issues


@dataclass
class TranslationOptions:
    api_key: str = field(default="", repr=False)
    api_keys: list = field(default_factory=list, repr=False)
    model: str = DEFAULT_MODEL
    provider: str = "gemini"  # v2.4.0: "gemini" | "openai" (gateway OpenAI-compatible)
    oa_base_url: str = DEFAULT_OA_BASE_URL  # v2.4.0: hanya dipakai provider "openai"
    oa_token_budget: int = 0  # v2.4.1: anggaran token sesi khusus "openai" (0 = mati); operasional, tidak masuk settings()
    style: str = DEFAULT_STYLE
    domain: str = "fiction"  # v2.3.0: "fiction" | "nonfiction" (prompt + namespace cache)
    ignore_cache: bool = False  # v2.3.0: generate ulang dari nol (abaikan cache; timpa output)
    glossary: dict[str, str] = field(default_factory=dict)
    custom_instruction: str = ""
    chunk_chars: int = 8000  # Accepted for old callers; never used for batching.
    max_requests: int = 490
    rpm: int = 13
    tpm: int = 248000
    rpd: int = 490
    target_tpm: int = 240000
    target_input_tokens: int = 30000
    max_input_tokens: int = 36000
    workers: int = 2
    auto_tune: bool = False
    token_count_mode: str = "auto"  # auto calibration / always / estimate
    max_output_tokens: int = 65536
    temperature: float = 0.2
    delay_seconds: float = 0.0
    max_retries: int = 5
    timeout: float = 600.0
    max_retry_wait: float = 120.0
    cache_dir: Path | None = None
    overwrite: bool = False
    epubcheck_jar: Path | None = None
    official_rpd: int = OFFICIAL_RPD  # Editable reference, not a server quota override.

    def keys(self) -> list:
        """Daftar API key ternormalisasi: api_keys bila diisi, sonst api_key lama.

        Key tidak pernah masuk settings()/cache identity: buku+pengaturan yang sama
        memakai cache yang sama walau key berbeda.
        """
        raw = []
        if isinstance(self.api_keys, list):
            raw.extend(self.api_keys)
        if isinstance(self.api_key, str) and self.api_key.strip():
            raw.append(self.api_key)
        out, seen = [], set()
        for key in raw:
            if not isinstance(key, str):
                continue
            key = key.strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @staticmethod
    def key_id(key: str) -> str:
        """Identitas key untuk akuntansi kuota. Digest, bukan key aslinya."""
        return digest(key)[:16]

    def key_label(self, key: str) -> str:
        """Label aman untuk UI/log: '#2 (abcd1234…)'."""
        try:
            index = self.keys().index(key) + 1
        except ValueError:
            index = 0
        return f"#{index} ({digest(key)[:8]}…)"

    def validate(self):
        if self.provider not in PROVIDERS:
            raise ValueError("Provider harus 'gemini' atau 'openai'.")
        self.model = self.model.removeprefix("models/").strip()
        if self.provider == "gemini":
            if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", self.model):
                raise ValueError("ID model Gemini tidak valid.")
        else:
            # ID model gateway OpenAI-compatible: token ASCII tercetak tanpa
            # spasi (mis. claude-sonnet-4-5, deepseek-3.2, gpt-5).
            if not isinstance(self.model, str) or not re.fullmatch(r"[!-~]{1,200}", self.model):
                raise ValueError("ID model OpenAI-compatible tidak valid.")
            self.oa_base_url = str(self.oa_base_url or "").strip().rstrip("/")
            if not (self.oa_base_url.startswith("https://")
                    or self.oa_base_url.startswith("http://127.0.0.1")
                    or self.oa_base_url.startswith("http://localhost")):
                raise ValueError("Base URL provider harus diawali https:// "
                                 "(http:// hanya untuk 127.0.0.1/localhost).")
        for name, lower, upper in (("chunk_chars", 2000, 24000),
                                   ("max_requests", 1, 10000), ("rpm", 1, OFFICIAL_RPM),
                                   ("tpm", 2000, OFFICIAL_TPM),
                                   ("target_tpm", 2000, OFFICIAL_TPM),
                                   ("target_input_tokens", 512, 40000),
                                   ("max_input_tokens", 512, 40000), ("workers", 1, 4),
                                   ("max_output_tokens", 2048, 65536),
                                   ("max_retries", 0, 8)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
                raise ValueError(f"{name} harus bilangan bulat {lower}–{upper}.")
        for name in ("official_rpd", "rpd"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} harus bilangan bulat positif.")
        for name, lower, upper in (("timeout", 5, 900), ("delay_seconds", 0, 300),
                                   ("temperature", 0, 2), ("max_retry_wait", 0, 300)):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} harus {lower}–{upper}.")
        if self.target_input_tokens > self.max_input_tokens:
            raise ValueError("Target input/request tidak boleh melebihi maksimum input/request.")
        if self.token_count_mode not in {"auto", "always", "estimate"}:
            raise ValueError("Mode token harus auto, always, atau estimate.")
        if self.domain not in DOMAINS:
            raise ValueError("Domain harus 'fiction' atau 'nonfiction'.")
        if not isinstance(self.ignore_cache, bool):
            raise ValueError("Generate ulang (ignore_cache) harus boolean.")
        if not isinstance(self.oa_token_budget, int) or isinstance(self.oa_token_budget, bool) or self.oa_token_budget < 0:
            raise ValueError("oa_token_budget harus bilangan bulat >= 0.")
        if not isinstance(self.auto_tune, bool):
            raise ValueError("Auto tune harus boolean.")
        keys = self.keys()
        if len(keys) > 10:
            raise ValueError("Maksimal 10 API key.")
        for key in keys:
            if len(key) > 500:
                raise ValueError("API key terlalu panjang.")
        if not isinstance(self.style, str) or not isinstance(self.custom_instruction, str):
            raise ValueError("Gaya dan instruksi harus berupa teks.")
        if len(self.style) > 2000 or len(self.custom_instruction) > 8000:
            raise ValueError("Gaya/instruksi terlalu panjang.")
        if not isinstance(self.glossary, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not k.strip() or not v.strip()
            for k, v in self.glossary.items()
        ) or len(canonical(self.glossary)) > 200000:
            raise ValueError("Glosarium harus pasangan teks yang tidak kosong, maksimal 200.000 karakter.")

    def settings(self):
        # Operational limits and batch sizes can change without losing resume.
        result = {"engine": ENGINE_VERSION, "prompt": PROMPT_VERSION,
                  "model": self.model, "style": self.style,
                  "glossary": self.glossary, "custom_instruction": self.custom_instruction,
                  "source": "en", "target": "id"}
        # v2.3.0: domain memengaruhi prompt, jadi domain non-default masuk
        # identitas cache. Default "fiction" SENGAJA tidak ditulis agar
        # job_id pengguna lama byte-identik dan cache mereka tetap terbaca.
        if self.domain != "fiction":
            result["domain"] = self.domain
        # v2.4.0: provider non-default masuk identitas cache dengan pola yang
        # sama, sehingga hasil gateway tidak pernah tercampur dengan hasil
        # Gemini. Default "gemini" SENGAJA tidak ditulis: settings/job_id
        # pengguna Gemini lama byte-identik dan cache 2.x tetap terbaca.
        if self.provider != "gemini":
            result["provider"] = self.provider
            result["oa_base_url"] = self.oa_base_url
        return result


@dataclass
class TranslationResult:
    complete: bool
    title: str
    total_units: int
    translated_units: int
    requests_made: int
    message: str
    primary_output: Path | None = None
    report_output: Path | None = None
    text_output: Path | None = None
    job_id: str = ""


def parse_glossary(raw: str) -> dict[str, str]:
    result = {}
    for index, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=>" if "=>" in line else "="
        if sep not in line:
            raise ValueError(f"Glosarium baris {index}: gunakan Inggris = Indonesia.")
        key, value = (s.strip() for s in line.split(sep, 1))
        if not key or not value or key in result:
            raise ValueError(f"Glosarium baris {index}: kosong atau istilah duplikat.")
        result[key] = value
    return result
