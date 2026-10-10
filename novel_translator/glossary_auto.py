"""Book Memory v1 — Autonomous Glossary: discovery, validasi, verifikasi, approval.

Alur per bab (publication barrier, dijalankan koordinator antar-bab):
  AI Discovery -> Local Validation (R1-R6, gratis) -> AI Verification
  -> Approval Router -> Persistent Book Memory

Tidak mengubah prompt/engine/cache/checkpoint penerjemahan. Injeksi glossary
tetap lewat ``TranslationOptions.glossary`` seperti glossary manual.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .book_memory import (
    BookMemory, BookMemoryStore, GlossaryTerm, _now,
    detect_conflicts, term_pattern,
)
from .storage import check_cancel

# Batas teks discovery per bab agar request tetap dalam anggaran.
DISCOVERY_MAX_CHARS = 60000
# Kategori yang boleh Auto Safe (konservatif: nama diri).
AUTO_SAFE_CATEGORIES = {"person", "place"}

DISCOVERY_INSTRUCTIONS = """You are a terminology extraction specialist for fiction translation.
Analyze the English source chapter below and extract FICTIONAL TERMINOLOGY —
terms that need consistent translation across a long novel.

Include:
- Proper nouns (people, places, organizations, factions)
- Fictional creatures, artifacts, abilities, classifications
- Multi-word terms ("beast weeds", "kraken coin")
- LOWERCASE fictional terms ("mazers", "sporting plants")
- Terms appearing only ONCE if they look like fictional terminology
- Titles, ranks, currencies, unique concepts

Do NOT include ordinary English vocabulary, even if capitalized at sentence start.
Do NOT invent terms not present in the text.
Do NOT force an Indonesian translation if the meaning is unclear — use null.
Each supporting_text must be an EXACT quote from the chapter (max 200 chars).

Return ONLY valid JSON:
{"terms": [{"source_term": "...", "category": "person|place|creature|faction|concept|artifact|organization|title",
"paragraph_id": "P12", "supporting_text": "exact quote", "proposed_indonesian": "... or null",
"ambiguity": "none or brief note"}]}"""

VERIFIER_INSTRUCTIONS = """You are a strict terminology verification auditor for English-to-Indonesian
fiction translation. You receive CANDIDATE term translations with their source
evidence. Your job is to VERIFY or REJECT each proposal — never rubber-stamp.

For EACH term, check:
1. Meaning fidelity: does the Indonesian proposal faithfully render the source term?
2. Additions: does the proposal add information NOT present in the source term
   or its evidence quote?
3. Omissions: does the proposal drop meaningful content from the source term?
4. Translate-vs-keep: proper nouns (names, places) are usually KEPT as-is;
   descriptive terms are translated. Flag wrong choices.
5. Misinterpretation: spelling errors, wrong transliteration, wrong word sense.
6. Consistency: compare against ESTABLISHED canonical translations below.
   A different rendering of the same source term is a CONFLICT unless justified.
7. If evidence is insufficient, say so — do not guess.

Verdict per term: "verified" (meets all checks), "provisional" (plausible but
evidence insufficient or minor concern), "ambiguous" (unresolved interpretation
or conflict).

Return ONLY valid JSON:
{"verdicts": [{"source_term": "...", "verdict": "verified|provisional|ambiguous",
"reasoning": "tied to the evidence quote", "issues": ["addition"|"omission"|
"spelling"|"transliteration"|"consistency"|"translate_vs_keep"|"insufficient_evidence"],
"corrected_translation": "better proposal or null (suggestion only, never auto-applied)"}]}

Be strict: a wrong canonical term approved now will propagate to hundreds of
chapters. When in doubt, choose provisional over verified."""


# ---------------------------------------------------------------------------
# Klien AI untuk discovery & verifikasi (melewati RateLimiter untuk kuota)
# ---------------------------------------------------------------------------

class GlossaryAIClient:
    """Klien JSON minimal untuk discovery/verifikasi via provider aktif.

    Memakai reservasi RateLimiter (kuota/RPM tercatat seperti request biasa).
    Koreksi AI tidak pernah diterapkan otomatis — hanya saran.
    """

    def __init__(self, options, limiter, cancel_event=None, status=None):
        if not options.keys():
            raise ValueError("API key belum diisi.")
        self.options = options
        self.limiter = limiter
        self.cancel_event = cancel_event
        self.status = status
        self.tokens_used = 0
        self.requests_made = 0

    def _estimate(self, text: str) -> int:
        return max(64, len(text) // 4)

    def complete_json(self, prompt: str, max_output: int = 8192) -> tuple[dict, int, int]:
        """Kirim prompt, kembalikan (parsed_json, in_tokens, out_tokens)."""
        import httpx
        check_cancel(self.cancel_event)
        estimated = self._estimate(prompt)
        provider = getattr(self.options, "provider", "gemini")
        if provider == "gemini":
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{self.options.model}:generateContent")
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_output,
                                         "temperature": 0.1,
                                         "responseMimeType": "application/json"}}
            headers = {"Content-Type": "application/json"}
            key_header = lambda k: {"x-goog-api-key": k}
        else:
            base = (getattr(self.options, "oa_base_url", "") or "").rstrip("/")
            url = f"{base}/chat/completions"
            body = {"model": self.options.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1, "max_tokens": max_output,
                    "response_format": {"type": "json_object"}}
            headers = {"Content-Type": "application/json"}
            key_header = lambda k: {"Authorization": f"Bearer {k}"}

        last = None
        for attempt in range(3):
            check_cancel(self.cancel_event)
            with self.limiter.request(estimated) as reservation:
                key = self.limiter.pinned_key(reservation)
                try:
                    with httpx.Client(timeout=120) as client:
                        resp = client.post(url, json=body,
                                           headers={**headers, **key_header(key)})
                except Exception as exc:
                    last = exc
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code == 429:
                    self.limiter.cooldown(15, key=key)
                    last = RuntimeError("rate limited (429)")
                    time.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Glossary AI HTTP {resp.status_code}")
                parsed = resp.json()
                # Rekonsiliasi usage SEBELUM parsing konten: HTTP 200 berarti
                # penyedia sudah menagih, apa pun hasil parsing JSON-nya.
                # Kegagalan pencatatan dilog via status callback, tidak
                # disembunyikan dengan except-pass.
                def _reconcile(note: str):
                    try:
                        self.limiter.usage(reservation, in_tok, out_tok)
                    except Exception as exc:
                        msg = (f"Gagal mencatat token aktual glossary "
                               f"({note}): {exc}")
                        if self.status:
                            try:
                                self.status(msg)
                            except Exception:
                                pass
                        # Simpan juga untuk diagnosis; jangan klaim sukses diam-diam.
                        self._usage_errors = getattr(self, "_usage_errors", [])
                        self._usage_errors.append(msg)
                try:
                    if provider == "gemini":
                        usage = parsed.get("usageMetadata", {})
                        in_tok = int(usage.get("promptTokenCount", estimated))
                        out_tok = int(usage.get("candidatesTokenCount", 0))
                    else:
                        usage = parsed.get("usage", {})
                        in_tok = int(usage.get("prompt_tokens", estimated))
                        out_tok = int(usage.get("completion_tokens", 0))
                except (ValueError, TypeError, AttributeError):
                    in_tok, out_tok = estimated, 0
                _reconcile("respons diterima")
                self.tokens_used += in_tok + out_tok
                self.requests_made += 1
                try:
                    if provider == "gemini":
                        text = parsed["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        text = parsed["choices"][0]["message"]["content"]
                    data = json.loads(text)
                    if isinstance(data, list):
                        data = {"terms": data}  # toleransi format
                    try:
                        self.limiter.usage(reservation, success=True)
                    except Exception as exc:
                        if self.status:
                            try:
                                self.status(f"Gagal menandai sukses limiter: {exc}")
                            except Exception:
                                pass
                    return data, in_tok, out_tok
                except (KeyError, IndexError, ValueError) as exc:
                    # JSON tidak valid: token SUDAH tercatat di atas (API menagih),
                    # tinggal retry untuk kontennya.
                    last = exc
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Glossary AI gagal setelah 3 percobaan: {last}")

    def discover(self, chapter_text: str, chapter_index: int) -> tuple[list[dict], int, int]:
        """Ekstraksi terminologi dari teks satu bab. Kembalikan (terms, in, out)."""
        text = chapter_text[:DISCOVERY_MAX_CHARS]
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        body = "\n\n".join(f"[P{i+1}] {p}" for i, p in enumerate(paras))
        prompt = (DISCOVERY_INSTRUCTIONS +
                  f"\n\nCHAPTER (index {chapter_index}):\n{body}")
        data, in_tok, out_tok = self.complete_json(prompt)
        terms = data.get("terms", []) if isinstance(data, dict) else []
        return terms, in_tok, out_tok

    def verify(self, candidates: list[dict], canonical: dict[str, str],
               batch_size: int = 5) -> tuple[list[dict], int, int]:
        """Verifikasi semantik per batch. Kembalikan (verdicts, in, out)."""
        canon_lines = "\n".join(f"- {k} -> {v}" for k, v in sorted(canonical.items()))
        context = ("\nESTABLISHED canonical translations (must stay consistent):\n"
                   + (canon_lines or "(none yet)"))
        verdicts, total_in, total_out = [], 0, 0
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            items = []
            for t in batch:
                items.append(
                    f"- term: \"{t['source_term']}\" [{t.get('category', '?')}] "
                    f"(chapter {t.get('chapter_index', '?')})\n"
                    f"  proposed: \"{t.get('proposed_indonesian')}\"\n"
                    f"  evidence: \"{(t.get('supporting_text') or '')[:300]}\"")
            prompt = VERIFIER_INSTRUCTIONS + context + "\nCANDIDATES:\n" + "\n".join(items)
            data, in_tok, out_tok = self.complete_json(prompt, max_output=4096)
            total_in += in_tok
            total_out += out_tok
            vlist = data.get("verdicts", []) if isinstance(data, dict) else data
            by_term = {v.get("source_term"): v for v in vlist if isinstance(v, dict)}
            for t in batch:
                v = by_term.get(t["source_term"], {})
                verdicts.append({"source_term": t["source_term"],
                                 "proposed": t.get("proposed_indonesian"),
                                 "verdict": v.get("verdict", "provisional"),
                                 "reasoning": v.get("reasoning", ""),
                                 "issues": v.get("issues", []),
                                 "corrected_translation": v.get("corrected_translation")})
        return verdicts, total_in, total_out


# ---------------------------------------------------------------------------
# Local Validator deterministik (R1-R6) — port dari eksperimen, tanpa API
# ---------------------------------------------------------------------------

_HONORIFICS = {"mr", "mrs", "ms", "dr", "sir", "lady", "professor"}
_EN_ID_MAP = {
    "mr": "tn", "mrs": "ny", "ms": "ny", "dr": "dr",
    "professor": "profesor", "hunter": "pemburu", "coin": "koin",
    "shop": "toko", "bronze": "perunggu", "hounds": "anjing",
    "hound": "anjing", "dragon": "naga", "fire": "api",
    "beasts": "binatang", "beast": "buas", "weeds": "gulma",
    "weed": "gulma", "capital": "ibu kota", "horse": "kuda",
    "curio": "barang kuno", "frontier": "perbatasan",
    "vampire": "vampir",
}
_ID_STOPWORDS = {
    "yang", "di", "ke", "dari", "dan", "atau", "untuk", "dengan", "pada",
    "adalah", "ini", "itu", "sebuah", "para", "sang", "si", "nya",
}
# Pemetaan gelar yang dibakukan — satu-satunya pengecualian R7.
# Nama diri lain yang diterjemahkan (bukan dipertahankan) adalah judgment call.
_TITLE_MAP = {
    "mr": "tn", "mrs": "ny", "ms": "ny", "dr": "dr",
    "professor": "profesor", "sir": "sir", "lady": "lady",
}


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-zà-ÿ]+", s.casefold())


def _norm_text(s: str) -> str:
    return " ".join(_tok(s or ""))


def _name_tokens(source_term: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-zÀ-ÿ']+", source_term)
            if w[0].isupper() and w.casefold() not in _HONORIFICS]


@dataclass
class LocalFinding:
    rule: str          # "R1".."R8"
    message: str


def validate_candidate(term: dict, chapter_text: str, seen: set[str],
                       approved: dict[str, str]) -> list[LocalFinding]:
    """Jalankan R1-R8. Kembalikan daftar temuan (kosong = bersih).

    term: {source_term, proposed_indonesian, supporting_text}.
    approved: glossary approved yang sudah ada (untuk R5).
    """
    findings: list[LocalFinding] = []
    src, prop = term.get("source_term", ""), term.get("proposed_indonesian") or ""

    # R1: kutipan bukti verbatim
    ev = _norm_text(term.get("supporting_text", ""))
    if not ev or _norm_text(chapter_text).find(ev) < 0:
        findings.append(LocalFinding("R1", "kutipan pendukung tidak verbatim di teks bab"))

    # R2: token nama dipertahankan / dipetakan baku
    prop_toks = set(_tok(prop))
    bad_names = [w for w in _name_tokens(src)
                 if w.casefold() not in prop_toks
                 and not (_EN_ID_MAP.get(w.casefold(), "") and
                          _EN_ID_MAP[w.casefold()] in prop.casefold())]
    if bad_names:
        findings.append(LocalFinding("R2", f"token nama hilang/berubah: {bad_names}"))

    # R3: tanpa tambahan tak didukung
    src_toks = set(_tok(src))
    allowed = " ".join(v for k, v in _EN_ID_MAP.items() if k in src_toks)
    bad_toks = [t for t in _tok(prop)
                if t not in _ID_STOPWORDS and t not in src_toks and t not in allowed]
    if bad_toks:
        findings.append(LocalFinding("R3", f"token tanpa dukungan sumber: {bad_toks}"))

    # R4: duplikat dalam set
    key = _norm_text(src)
    if key in seen:
        findings.append(LocalFinding("R4", "istilah sumber duplikat dalam set"))
    else:
        seen.add(key)

    # R5: konflik dengan approved glossary (manual selalu menang)
    for ak, av in approved.items():
        if _norm_text(ak) == key and prop.strip().casefold() != av.strip().casefold():
            findings.append(LocalFinding(
                "R5", f"konflik glossary: approved='{av}' vs usulan='{prop}'"))
            break

    # R6: transliterasi mencurigakan (mirip tapi bukan pemetaan baku)
    prop_words = re.findall(r"[A-Za-zÀ-ÿ']+", prop)
    for w in _name_tokens(src):
        wl = w.casefold()
        if wl in [p.casefold() for p in prop_words]:
            continue
        if _EN_ID_MAP.get(wl, "") and _EN_ID_MAP[wl] in prop.casefold():
            continue
        for p in prop_words:
            r = SequenceMatcher(None, wl, p.casefold()).ratio()
            if 0.6 < r < 1.0:
                findings.append(LocalFinding("R6", f"transliterasi mencurigakan: {w}->{p}"))
                break

    # R7: proper noun yang DITERJEMAHKAN (bukan dipertahankan) -> butuh review.
    # Konservatif & umum (bukan overfit satu contoh): nama diri dengan makna
    # leksikal ("Frontier"->"Perbatasan", "Capital"->"Ibu Kota") adalah
    # judgment call — jangan auto-approve tanpa keputusan manusia.
    # Kategori creature/artifact/concept lowercase tidak tersentuh (memang
    # untuk diterjemahkan); pengecualian hanya gelar baku (_TITLE_MAP).
    for w in _name_tokens(src):
        wl = w.casefold()
        if wl in [p.casefold() for p in prop_words]:
            continue  # dipertahankan apa adanya -> aman
        if _TITLE_MAP.get(wl, "") and _TITLE_MAP[wl] in prop.casefold():
            continue  # gelar baku -> aman
        findings.append(LocalFinding(
            "R7", f"proper noun diterjemahkan tanpa dipertahankan: '{w}' — butuh review"))
        break

    # R8: istilah sumber HARUS muncul dalam supporting_text yang valid.
    # Mencegah bukti "tempel": istilah ada di kalimat lain bab tersebut,
    # tetapi kutipan bukti membahas hal berbeda. Untuk Auto Safe, bukti
    # harus benar-benar membuktikan istilah yang diklaim.
    # (R1 memastikan kutipan verbatim di bab; R8 memastikan istilah ada
    # di DALAM kutipan itu, bukan di tempat lain.)
    if src.strip():
        ev_raw = term.get("supporting_text", "") or ""
        if not term_pattern(src).search(ev_raw):
            findings.append(LocalFinding(
                "R8", f"istilah sumber tidak ada dalam kutipan bukti: '{src}'"))
    return findings


# ---------------------------------------------------------------------------
# Approval Router
# ---------------------------------------------------------------------------

@dataclass
class ApprovalDecision:
    approved: bool
    status: str  # "verified" | "provisional" | "ambiguous" | "rejected"
    origin: str  # "auto" | "manual" | "pending"
    reasons: list[str] = field(default_factory=list)


def route_approval(candidate: dict, local_findings: list[LocalFinding],
                  ai_verdict: dict, approved_glossary: dict[str, str],
                  rejected: set[str], policy: str) -> ApprovalDecision:
    """Tentukan nasib satu kandidat.

    policy: "auto_safe" | "review_first".
    - Manual approved glossary selalu menang (konflik -> kandidat ditahan).
    - Istilah yang pernah di-rejected user tidak diusulkan ulang otomatis.
    - Koreksi AI tidak pernah diterapkan otomatis.
    """
    src = candidate.get("source_term", "")
    key = _norm_text(src)
    if key in rejected:
        return ApprovalDecision(False, "rejected", "pending",
                                ["pernah ditolak user — tidak diusulkan ulang otomatis"])
    # konflik dengan approved (R5) -> selalu tahan
    if any(f.rule == "R5" for f in local_findings):
        return ApprovalDecision(False, "ambiguous", "pending",
                                ["konflik dengan Approved Glossary — butuh keputusan manual"])
    verdict = (ai_verdict or {}).get("verdict", "provisional")
    if verdict == "ambiguous":
        return ApprovalDecision(False, "ambiguous", "pending",
                                ["AI verifier: ambiguous — " + str((ai_verdict or {}).get("reasoning", ""))[:120]])
    if policy == "review_first":
        return ApprovalDecision(False, verdict if verdict in ("verified", "provisional") else "provisional",
                                "pending", ["mode Review First — menunggu persetujuan user"])
    # --- Auto Safe: semua syarat harus terpenuhi ---
    reasons = []
    if local_findings:
        reasons.append("local findings: " + ", ".join(f.rule for f in local_findings))
    if verdict != "verified":
        reasons.append(f"AI verdict bukan verified ({verdict})")
    category = (candidate.get("category") or "").casefold()
    if category not in AUTO_SAFE_CATEGORIES:
        reasons.append(f"kategori '{category}' di luar Auto Safe")
    if (ai_verdict or {}).get("corrected_translation"):
        # ada saran koreksi -> jangan auto-approve; manusia yang memutuskan
        reasons.append("AI menyarankan koreksi — butuh review")
    if reasons:
        return ApprovalDecision(False, verdict if verdict != "verified" else "provisional",
                                "pending", reasons)
    return ApprovalDecision(True, "verified", "auto", ["Auto Safe: seluruh syarat terpenuhi"])


# ---------------------------------------------------------------------------
# Chapter grouping & job identity (untuk chronological learning)
# ---------------------------------------------------------------------------

def group_chapters(book) -> list[tuple[int, list]]:
    """Kelompokkan unit per dokumen spine (1-based), sesuai urutan dokumen.

    Identifikasi bab memakai ``unit.document`` — urutan spine EPUB adalah
    sumber kebenaran. Fallback aman: bila tak ada pemetaan, seluruh buku
    menjadi satu bab.
    """
    try:
        order = list(book.documents.keys())
    except AttributeError:
        order = []
    buckets: dict[str, list] = {d: [] for d in order}
    for u in book.units:
        doc = getattr(u, "document", None)
        if doc in buckets:
            buckets[doc].append(u)
        else:
            buckets.setdefault(doc or "", []).append(u)
    chapters = [(i + 1, buckets[d]) for i, d in enumerate(buckets) if buckets[d]]
    return chapters or [(1, list(book.units))]


def chapter_job_id(base_job_id: str, chapter_index: int,
                   glossary: dict[str, str] | None = None) -> str:
    """Namespace cache per bab: deterministik, tanpa tabrakan antar-bab.

    Glosari efektif (auto-approved + manual) di-hash ke dalam ID sehingga
    perubahan glossary TIDAK diam-diam memakai cache lama yang tidak kompatibel.
    Namespace lama dipertahankan (tidak dihapus); perubahan glossary membuat
    namespace baru dan bab diterjemahkan ulang dengan glossary yang benar.
    """
    import hashlib
    h = hashlib.sha256(f"{base_job_id}|autochapter|{chapter_index}".encode("utf-8"))
    if glossary:
        # Hash deterministik: urutkan pasangan istilah (case-insensitive).
        items = sorted((k.casefold(), v) for k, v in glossary.items() if k and v)
        h.update(repr(items).encode("utf-8"))
    return "ch" + h.hexdigest()[:16]


def effective_chapter_glossary(memory, chapter_index: int,
                               manual_glossary: dict[str, str]) -> dict[str, str]:
    """Glosari efektif untuk satu bab: auto-approved bab sebelumnya + manual.

    Manual approved SELALU menang, termasuk normalisasi kapitalisasi
    ("toto" manual mengalahkan "Toto" auto).
    """
    glossary = memory.glossary_upto_chapter(chapter_index)
    manual_norm = {k.casefold(): k for k in (manual_glossary or {})}
    for k in list(glossary):
        if k.casefold() in manual_norm:
            del glossary[k]
    glossary.update(manual_glossary or {})
    return glossary


# ---------------------------------------------------------------------------
# Orkestrasi publication barrier per bab
# ---------------------------------------------------------------------------

@dataclass
class ChapterDiscoveryResult:
    chapter_index: int
    discovered: int = 0
    auto_approved: int = 0
    pending: int = 0
    rejected: int = 0
    api_tokens: int = 0
    api_requests: int = 0
    terms: list[str] = field(default_factory=list)


def _chapter_text(units) -> str:
    parts = []
    for u in units:
        text = "".join((r.prefix or "") + r.text + (r.suffix or "") for r in u.runs)
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def run_chapter_discovery(chapter_index: int, units, memory: BookMemory,
                          store: BookMemoryStore, ai_client: GlossaryAIClient | None,
                          policy: str, enable_verification: bool,
                          cancel_event=None,
                          manual_glossary: dict[str, str] | None = None) -> ChapterDiscoveryResult:
    """Publication barrier: discovery -> validasi -> verifikasi -> approval.

    Idempoten: bila bab sudah tercatat published di discovery_log, lewati.

    manual_glossary: glossary manual dari TranslationOptions. Digabungkan ke
    approved untuk R5 (manual menang) dan ke canonical untuk verifier, sehingga
    Auto Safe tidak menyimpan padanan yang bertentangan dengan instruksi manual.
    """
    result = ChapterDiscoveryResult(chapter_index=chapter_index)
    log_key = str(chapter_index)
    if memory.discovery_log.get(log_key, {}).get("status") == "published":
        return result  # sudah dikerjakan — idempoten
    check_cancel(cancel_event)

    text = _chapter_text(units)
    if not text.strip():
        memory.discovery_log[log_key] = {"status": "published", "n_terms": 0,
                                         "at": _now(), "note": "bab kosong"}
        store.save(memory)
        return result

    # --- 1. AI Discovery ---
    if ai_client is None:
        memory.discovery_log[log_key] = {"status": "published", "n_terms": 0,
                                         "at": _now(), "note": "discovery nonaktif"}
        store.save(memory)
        return result
    terms, in_tok, out_tok = ai_client.discover(text, chapter_index)
    result.api_tokens += in_tok + out_tok
    result.api_requests += 1
    memory.record_api_usage("discovery", in_tok + out_tok, 1)

    # --- 2. Local Validation (gratis) ---
    # Gabungkan manual (menang) untuk deteksi konflik R5.
    approved_now = memory.glossary_dict(approved_only=True)
    _mn = {k.casefold(): k for k in (manual_glossary or {})}
    for _k in list(approved_now):
        if _k.casefold() in _mn:
            del approved_now[_k]
    approved_now.update(manual_glossary or {})
    seen: set[str] = set()
    enriched = []
    for t in terms:
        if not t.get("source_term"):
            continue
        t["chapter_index"] = chapter_index
        findings = validate_candidate(t, text, seen, approved_now)
        enriched.append((t, findings))

    # --- 3. AI Verification (opsional) ---
    verdicts: dict[str, dict] = {}
    if enable_verification and enriched:
        cands = [t for t, _ in enriched]
        canonical = {k: v for k, v in approved_now.items()}
        vlist, vin, vout = ai_client.verify(cands, canonical)
        result.api_tokens += vin + vout
        result.api_requests += (len(cands) + 4) // 5
        memory.record_api_usage("verification", vin + vout, (len(cands) + 4) // 5)
        verdicts = {v["source_term"]: v for v in vlist}

    # --- 4. Approval Router + persist ---
    rejected = set(memory.rejected_terms)
    for t, findings in enriched:
        check_cancel(cancel_event)
        verdict = verdicts.get(t["source_term"], {"verdict": "provisional",
                                                  "reasoning": "verifikasi nonaktif",
                                                  "issues": [], "corrected_translation": None})
        decision = route_approval(t, findings, verdict, approved_now, rejected, policy)
        key = " ".join(t["source_term"].split())
        # hindari duplikat terhadap istilah yang sudah ada
        existing = next((e for e in memory.terms
                         if e.normalized() == " ".join(key.casefold().split())), None)
        if existing is not None:
            continue  # sudah dikenal — idempoten
        term = GlossaryTerm(
            term=key,
            translation=(t.get("proposed_indonesian") or "").strip(),
            approved=decision.approved,
            note="; ".join(decision.reasons),
            status=decision.status,
            origin=decision.origin if decision.approved else "pending",
            source_chapter=chapter_index,
            evidence=(t.get("supporting_text") or "")[:300],
        )
        if decision.status == "rejected":
            memory.rejected_terms.append(term.normalized())
            result.rejected += 1
        else:
            memory.terms.append(term)
            result.terms.append(key)
            if decision.approved:
                result.auto_approved += 1
            else:
                result.pending += 1
        result.discovered += 1

    memory.discovery_log[log_key] = {
        "status": "published", "n_terms": result.discovered,
        "auto_approved": result.auto_approved, "pending": result.pending,
        "api_tokens": result.api_tokens, "at": _now()}
    store.save(memory)
    return result
