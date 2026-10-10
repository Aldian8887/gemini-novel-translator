"""Book Memory v0 — penyimpanan terminologi permanen per buku.

Lapisan penyimpanan di atas mekanisme glossary yang sudah ada di pipeline.
TIDAK mengubah prompt, engine, cache, atau checkpoint:
- injeksi tetap lewat ``TranslationOptions.glossary`` (dict) — byte-identik
  dengan glossary manual bila isinya sama;
- glossary sudah menjadi bagian cache identity (settings/job_id), sehingga
  perubahan glossary otomatis memakai namespace cache baru yang aman.

Fitur v0:
- identitas buku stabil dari konten sumber (bukan metadata),
- CRUD istilah + penandaan approved,
- deteksi konflik & duplikasi,
- export format kompatibel ``--glossary`` CLI,
- daftar unit terdampak saat glossary berubah di tengah proyek,
- validasi terminologi pasca-terjemahan (lapor, tanpa retry otomatis),
- character notes manual & opsional (tanpa klaim kualitas).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .storage import atomic_bytes

BOOK_MEMORY_VERSION = 1
DEFAULT_ROOT = Path.home() / ".novel_translator" / "book_memory"
# Batas injeksi character notes agar overhead token terkendali (v0).
CHARACTER_BLOCK_MAX_CHARS = 1500


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def epub_text_blocks(epub_path: str | Path) -> list[str]:
    """LEGASI: ekstraksi blok via regex — TIDAK dipakai validasi lagi.

    Digantikan validate_epub_pair() yang memakai EpubBook di kedua sisi.
    Dipertahankan agar tidak merusak pemanggil eksternal; jangan pakai untuk
    penjajaran sumber<->hasil (pernah meleset: 9 unit vs 8 blok).
    """
    """Ambil semua blok teks (judul + paragraf) sebuah EPUB sesuai urutan dokumen.

    Dipakai untuk penjajaran sumber<->hasil yang robust: unit judul ikut
    dihitung agar tidak terjadi off-by-one.
    """
    import zipfile as _zipfile
    z = _zipfile.ZipFile(str(epub_path))
    # cari dokumen xhtml pertama di spine (atau semua, berurutan)
    names = [n for n in z.namelist() if n.endswith((".xhtml", ".html"))]
    blocks: list[str] = []
    for name in names:
        try:
            x = z.read(name).decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            continue
        # satu regex sesuai urutan dokumen
        for m in re.finditer(r"<(title|p|h1|h2|h3)[^>]*>(.*?)</\1>", x, re.S):
            text = re.sub(r"<[^>]+>", "", m.group(2))
            text = " ".join(text.split())
            if text:
                blocks.append(text)
    return blocks


def parse_known_from(value: str) -> int | None:
    """Urai 'bab 3' / 'bab 2-5' / '3' -> nomor bab mulai dikenal (int).

    Kosong -> None (selalu boleh). Tak terurai -> None (diperlakukan sebagai
    selalu-boleh, tetapi pemanggil sebaiknya mencatat peringatan).
    """
    value = (value or "").strip().casefold()
    if not value:
        return None
    m = re.search(r"(\d+)", value)
    return int(m.group(1)) if m else None


def notes_for_chapter(notes: list[CharacterNote],
                      chapter: int | None) -> tuple[list[CharacterNote], list[str]]:
    """Saring catatan karakter agar info dari bab SELANJUTNYA tidak bocor ke
    bab sebelumnya. Kembalikan (notes_lolos, peringatan).

    - chapter=None -> semua lolos (tanpa penyaringan).
    - known_from kosong/tak-terurai -> lolos + peringatan (konservatif-terbuka,
      didokumentasikan; user didorong mengisi format 'bab N').
    """
    allowed, warnings = [], []
    for n in notes:
        start = parse_known_from(n.known_from)
        if n.known_from.strip() and start is None:
            warnings.append(f"'{n.name}': known_from '{n.known_from}' tak terurai — diloloskan.")
            allowed.append(n)
        elif start is None or chapter is None or chapter >= start:
            allowed.append(n)
        # else: ditahan — info dari bab depan
    return allowed, warnings


def _cli() -> int:
    """CLI kecil untuk audit & power user. Contoh:
    python -m novel_translator.book_memory bookid buku.epub
    python -m novel_translator.book_memory export bm_xxx --out g.txt
    python -m novel_translator.book_memory validate --src a.epub --out b_ID.epub --glossary g.txt
    """
    import argparse
    ap = argparse.ArgumentParser(prog="book_memory", description="Book Memory v0 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bookid", help="Hitung identitas stabil sebuah EPUB")
    p.add_argument("epub")
    p = sub.add_parser("export", help="Ekspor glossary approved ke format --glossary")
    p.add_argument("book_id"); p.add_argument("--out", required=True)
    p = sub.add_parser("import", help="Impor glossary dari file txt/json")
    p.add_argument("book_id"); p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--title", default="")
    p = sub.add_parser("validate", help="Validasi terminologi hasil terjemahan")
    p.add_argument("--src", required=True); p.add_argument("--out", required=True)
    p.add_argument("--glossary", required=True,
                   help="File txt 'Inggris = Indonesia' (lihat export)")
    p = sub.add_parser("affected", help="Unit terdampak perubahan glossary")
    p.add_argument("--epub", required=True)
    p.add_argument("--old", required=True); p.add_argument("--new", required=True)
    p = sub.add_parser("list", help="Daftar buku di Book Memory")
    args = ap.parse_args()
    store = BookMemoryStore()
    if args.cmd == "bookid":
        print(compute_book_id(args.epub))
    elif args.cmd == "export":
        print(store.export_glossary_file(args.book_id, args.out))
    elif args.cmd == "import":
        from .models import parse_glossary
        raw = Path(args.inp).read_text(encoding="utf-8")
        if args.inp.endswith(".json"):
            data = json.loads(raw)
            items = data.get("terms", data) if isinstance(data, dict) else data
            pairs = [(str(i.get("term", "")), str(i.get("translation", ""))) for i in items]
        else:
            pairs = list(parse_glossary(raw).items())
        mem = store.load(args.book_id)
        if args.title:
            mem.title = args.title
        mem.terms = [GlossaryTerm(term=k, translation=v) for k, v in pairs if k and v]
        store.save(mem)
        print(f"diimpor: {len(mem.terms)} istilah -> {args.book_id}")
    elif args.cmd == "validate":
        from .models import parse_glossary
        glossary = parse_glossary(Path(args.glossary).read_text(encoding="utf-8"))
        res = validate_epub_pair(args.src, args.out, glossary)
        print(f"status: {res.status}" + (f" ({res.detail})" if res.detail else ""))
        for v in res.violations:
            print(f"{v.unit_id}: {v.detail}")
        print(f"pelanggaran: {len(res.violations)} / {res.units_checked} unit diperiksa")
    elif args.cmd == "affected":
        from .models import parse_glossary
        from .epub import EpubBook
        old = [GlossaryTerm(term=k, translation=v)
               for k, v in parse_glossary(Path(args.old).read_text(encoding="utf-8")).items()]
        new = [GlossaryTerm(term=k, translation=v)
               for k, v in parse_glossary(Path(args.new).read_text(encoding="utf-8")).items()]
        units = EpubBook(args.epub).units
        aff = units_affected_by_change(old, new, units)
        print("\n".join(aff) if aff else "(tidak ada)")
        print(f"unit terdampak: {len(aff)}")
    elif args.cmd == "list":
        for b in store.list_books():
            print(f"{b['book_id']}  {b['title']}  ({b['terms']} istilah, {b['characters']} karakter)")
    return 0


def _norm_term(term: str) -> str:
    return " ".join(term.strip().split()).casefold()


def term_pattern(term: str) -> "re.Pattern[str]":
    """Regex yang sama dengan PromptBuilder: word-boundary, case-insensitive."""
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in term.split())
                      + r"(?!\w)", re.IGNORECASE)


def compute_book_id(epub_path: str | Path) -> str:
    """Identitas stabil dari KONTEN sumber (bukan metadata/nama file).

    Dua file EPUB dari buku yang sama (beda metadata/kompresi) menghasilkan
    ID yang sama; buku berbeda menghasilkan ID berbeda.
    """
    from .epub import EpubBook  # impor lokal: hindari siklus impor
    book = EpubBook(str(epub_path))
    h = hashlib.sha256()
    for unit in book.units:
        text = "".join((r.prefix or "") + r.text + (r.suffix or "") for r in unit.runs)
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
    return "bm_" + h.hexdigest()[:16]


@dataclass
class GlossaryTerm:
    term: str
    translation: str
    approved: bool = True
    note: str = ""
    updated_at: str = field(default_factory=_now)

    def normalized(self) -> str:
        return _norm_term(self.term)


@dataclass
class CharacterNote:
    """Catatan manual & opsional. Tanpa klaim kualitas sebelum ada benchmark.

    Batasan v0: hanya identitas, variasi nama, sapaan/gelar, hubungan yang
    SUDAH terkonfirmasi (ditulis user, bukan spekulasi AI), dan catatan register
    dialog yang disetujui user. Jangan masukkan info dari bab selanjutnya bila
    catatan dipakai untuk bab sebelumnya — disiplin ini di tangan user, dibantu
    field ``known_from``.
    """
    name: str
    aliases: list[str] = field(default_factory=list)
    title: str = ""
    relationships: str = ""
    register_note: str = ""
    known_from: str = ""  # mis. "bab 1-3" — cegah spoiler bab selanjutnya
    updated_at: str = field(default_factory=_now)


@dataclass
class BookMemory:
    book_id: str
    title: str = ""
    terms: list[GlossaryTerm] = field(default_factory=list)
    characters: list[CharacterNote] = field(default_factory=list)
    version: int = BOOK_MEMORY_VERSION

    def glossary_dict(self, approved_only: bool = True) -> dict[str, str]:
        """Dict siap pakai untuk ``TranslationOptions.glossary``."""
        result = {}
        for t in self.terms:
            if approved_only and not t.approved:
                continue
            if t.term and t.translation and t.term not in result:
                result[t.term] = t.translation
        return result

    def character_block(self, max_chars: int = CHARACTER_BLOCK_MAX_CHARS,
                          chapter: int | None = None) -> str:
        """Blok ringkas untuk injeksi opsional. Dibatasi agar overhead kecil.

        Bila chapter diisi, hanya catatan dengan known_from <= chapter yang
        disertakan (anti-spoiler bab depan).
        """
        notes, _warnings = notes_for_chapter(self.characters, chapter)
        if not notes:
            return ""
        lines = ["Catatan karakter (referensi; jangan sisipkan bila tak relevan):"]
        for c in notes:
            parts = [c.name]
            if c.aliases:
                parts.append("alias: " + ", ".join(c.aliases))
            if c.title:
                parts.append("sapaan/gelar: " + c.title)
            if c.relationships:
                parts.append("hubungan: " + c.relationships)
            if c.register_note:
                parts.append("register: " + c.register_note)
            lines.append("- " + "; ".join(parts))
        text = "\n".join(lines)
        if len(text) > max_chars:
            cut = text[:max_chars].rsplit("\n", 1)[0]
            if len(cut) < max_chars // 2:
                cut = text[:max_chars]  # satu baris panjang: potong keras
            text = cut + "\n…(dipotong)"
        return text


@dataclass
class Conflict:
    kind: str  # "duplicate" | "overlap" | "translation_mismatch"
    terms: list[str]
    message: str


def detect_conflicts(terms: list[GlossaryTerm]) -> list[Conflict]:
    """Deteksi duplikasi, overlap substring, dan terjemahan bentrok."""
    conflicts: list[Conflict] = []
    seen: dict[str, GlossaryTerm] = {}
    for t in terms:
        key = t.normalized()
        if not key:
            continue
        if key in seen:
            other = seen[key]
            if _norm_term(other.translation) != _norm_term(t.translation):
                conflicts.append(Conflict(
                    "translation_mismatch", [other.term, t.term],
                    f"'{other.term}' punya dua terjemahan: '{other.translation}' vs '{t.translation}'."))
            else:
                conflicts.append(Conflict(
                    "duplicate", [other.term, t.term],
                    f"Istilah duplikat: '{other.term}' ≈ '{t.term}'."))
        else:
            seen[key] = t
    keys = list(seen)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            # overlap: satu istilah menjadi frasa di dalam istilah lain
            if re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", b) or \
               re.search(r"(?<!\w)" + re.escape(b) + r"(?!\w)", a):
                conflicts.append(Conflict(
                    "overlap", [seen[a].term, seen[b].term],
                    f"Istilah tumpang tindih: '{seen[a].term}' vs '{seen[b].term}'. "
                    f"Pertimbangkan memakai yang lebih panjang/spesifik."))
    return conflicts


class BookMemoryStore:
    """Penyimpanan JSON per buku, atomic write, tanpa dependensi baru."""

    def __init__(self, root: str | Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, book_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", book_id)
        return self.root / f"{safe}.json"

    def load(self, book_id: str) -> BookMemory:
        path = self._path(book_id)
        if not path.exists():
            return BookMemory(book_id=book_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return BookMemory(
            book_id=data.get("book_id", book_id),
            title=data.get("title", ""),
            terms=[GlossaryTerm(**t) for t in data.get("terms", [])],
            characters=[CharacterNote(**c) for c in data.get("characters", [])],
            version=data.get("version", 1))

    def save(self, memory: BookMemory) -> None:
        payload = {"book_id": memory.book_id, "title": memory.title,
                   "version": BOOK_MEMORY_VERSION,
                   "terms": [asdict(t) for t in memory.terms],
                   "characters": [asdict(c) for c in memory.characters]}
        atomic_bytes(self._path(memory.book_id),
                     json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
                     overwrite=True)

    def list_books(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("bm_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append({"book_id": data.get("book_id", path.stem),
                               "title": data.get("title", ""),
                               "terms": len(data.get("terms", [])),
                               "characters": len(data.get("characters", []))})
            except (OSError, ValueError):
                continue
        return result

    def export_glossary_file(self, book_id: str, dest: str | Path,
                             approved_only: bool = True) -> Path:
        """Tulis format ``Inggris = Indonesia`` per baris — kompatibel
        dengan flag CLI ``--glossary`` yang sudah ada."""
        memory = self.load(book_id)
        lines = [f"{k} = {v}" for k, v in memory.glossary_dict(approved_only).items()]
        dest = Path(dest)
        atomic_bytes(dest, ("\n".join(lines) + "\n").encode("utf-8"), overwrite=True)
        return dest


def changed_terms(old: list[GlossaryTerm],
                  new: list[GlossaryTerm]) -> dict[str, list[str]]:
    """Bandingkan dua daftar istilah; kembalikan term yang tambah/hapus/ubah.

    Perubahan status approved ON/OFF dihitung sebagai perubahan efektif
    (mempengaruhi glossary_dict yang diinjeksikan ke model).
    """
    def sig(t: GlossaryTerm):
        return (_norm_term(t.translation), bool(t.approved))
    old_map = {_norm_term(t.term): sig(t) for t in old if t.term}
    new_map = {_norm_term(t.term): sig(t) for t in new if t.term}
    added = [t.term for t in new if _norm_term(t.term) not in old_map]
    removed = [t.term for t in old if _norm_term(t.term) not in new_map]
    changed = [t.term for t in new
               if _norm_term(t.term) in old_map
               and new_map[_norm_term(t.term)] != old_map[_norm_term(t.term)]]
    return {"added": added, "removed": removed, "changed": changed}


def units_affected_by_change(old_terms: list[GlossaryTerm], new_terms: list[GlossaryTerm],
                             units) -> list[str]:
    """Unit yang sumbernya cocok dengan istilah yang tambah/hapus/ubah.

    Mekanisme aman untuk menjawab: 'apakah bagian lama perlu diterjemahkan ulang?'
    """
    diff = changed_terms(old_terms, new_terms)
    watch = diff["added"] + diff["removed"] + diff["changed"]
    if not watch:
        return []
    patterns = [(term, term_pattern(term)) for term in watch]
    affected = []
    for unit in units:
        text = "".join((r.prefix or "") + r.text + (r.suffix or "") for r in unit.runs)
        if any(p.search(text) for _, p in patterns):
            affected.append(unit.id)
    return affected


@dataclass
class TermViolation:
    unit_id: str
    term: str
    expected: str
    detail: str


@dataclass
class ValidationResult:
    violations: list[TermViolation]
    status: str  # "verified" | "unverifiable"
    detail: str = ""
    units_checked: int = 0


def _count_occurrences(pattern_text: str, text: str) -> int:
    return len(re.findall(r"(?<!\w)" + re.escape(pattern_text) + r"(?!\w)",
                         text, re.IGNORECASE))


def validate_glossary(units, translations: dict[str, str],
                      glossary: dict[str, str]) -> list[TermViolation]:
    """Validasi pasca-terjemahan (heuristik, lapor saja — tanpa retry otomatis).

    Aturan: bila sumber cocok dengan istilah, terjemahan harus memuat padanan
    approved (case-insensitive, word-boundary) minimal sebanyak kemunculan
    sumber. Ini menangkap kasus "Hunter met Hunter" -> "Sang Pemburu bertemu
    Hunter" yang lolos dari cek substring biasa.
    Batasan yang didokumentasikan: substitusi pronomina yang sah
    ("Hunter ... Hunter" -> "Pemburu ... ia") dapat tertandai; ini temuan untuk
    ditinjau manusia, bukan vonis otomatis. Bukan validasi semantik sempurna.
    """
    violations: list[TermViolation] = []
    # Istilah terpanjang dulu; span yang sudah tercakup istilah lebih panjang
    # tidak dilaporkan ganda (mis. "Vampire Hunter" menutupi "Hunter").
    patterns = sorted(
        ((term, trans, term_pattern(term)) for term, trans in glossary.items()
         if term and trans),
        key=lambda t: len(t[0]), reverse=True)
    for unit in units:
        src = "".join((r.prefix or "") + r.text + (r.suffix or "") for r in unit.runs)
        tgt = translations.get(unit.id, "")
        if not tgt:
            continue
        covered: list[tuple[int, int]] = []
        for term, trans, pattern in patterns:
            spans = [m.span() for m in pattern.finditer(src)
                     if not any(c[0] <= m.span()[0] and m.span()[1] <= c[1]
                                for c in covered)]
            if not spans:
                continue
            src_count = len(spans)
            tgt_count = _count_occurrences(trans, tgt)
            if tgt_count < src_count:
                violations.append(TermViolation(
                    unit_id=unit.id, term=term, expected=trans,
                    detail=f"sumber memuat '{term}' {src_count}x tetapi padanan "
                           f"'{trans}' hanya {tgt_count}x"))
            for s in spans:
                covered.append(s)
    return violations


def _strip_lang_for_validation(epub_path: str | Path, dest: str | Path) -> None:
    """Salinan EPUB tanpa atribut xml:lang/lang — HANYA untuk validasi.

    EpubBook menolak mengekstrak dokumen yang dideklarasikan non-Inggris
    (output pipeline selalu bertanda bahasa target). Atribut bahasa tidak
    memengaruhi unitisasi struktural, jadi penjajaran tetap valid.
    """
    import zipfile
    from xml.etree import ElementTree as ET
    XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
    with zipfile.ZipFile(epub_path) as zin, zipfile.ZipFile(dest, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith((".xhtml", ".html", ".htm")):
                try:
                    root = ET.fromstring(data)
                    for el in root.iter():
                        el.attrib.pop(XML_LANG, None)
                        el.attrib.pop("lang", None)
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                except ET.ParseError:
                    pass
            zout.writestr(item, data)


def validate_epub_pair(source_epub: str | Path, output_epub: str | Path,
                       glossary: dict[str, str]) -> ValidationResult:
    """Validasi memakai parser STRUKTURAL yang sama (EpubBook) di kedua sisi.

    Penjajaran memakai identitas struktural yang dapat diverifikasi (jumlah unit
    + urutan dokumen). Bila tidak dapat dipastikan, kembalikan status
    "unverifiable" — JANGAN mengklaim semua istilah konsisten.
    """
    import tempfile
    from .epub import EpubBook  # impor lokal: hindari siklus impor
    tmp_out = None
    try:
        try:
            sunits = EpubBook(str(source_epub)).units
        except Exception as e:
            return ValidationResult([], "unverifiable", f"gagal parse sumber: {e}")
        # Output pipeline bertanda xml:lang=target -> EpubBook menolak ekstrak;
        # normalisasi salinan sementara (struktur tidak berubah).
        tmp_out = tempfile.NamedTemporaryFile(suffix=".epub", delete=False).name
        _strip_lang_for_validation(output_epub, tmp_out)
        try:
            ounits = EpubBook(tmp_out).units
        except Exception as e:
            return ValidationResult([], "unverifiable", f"gagal parse hasil: {e}")
    finally:
        if tmp_out:
            Path(tmp_out).unlink(missing_ok=True)
    if len(sunits) != len(ounits):
        return ValidationResult(
            [], "unverifiable",
            f"jumlah unit sumber ({len(sunits)}) != hasil ({len(ounits)}); "
            f"penjajaran tidak dapat dipastikan")
    if [u.document for u in sunits] != [u.document for u in ounits]:
        return ValidationResult([], "unverifiable", "urutan dokumen berbeda")
    trans = {u.id: "".join((r.prefix or "") + r.text + (r.suffix or "")
                           for r in ounits[i].runs)
             for i, u in enumerate(sunits)}
    return ValidationResult(validate_glossary(sunits, trans, glossary),
                            "verified", "", len(sunits))


if __name__ == "__main__":
    raise SystemExit(_cli())
