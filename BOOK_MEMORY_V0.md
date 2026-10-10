# Book Memory v0 — Desain & Implementasi

Status: **DIIMPLEMENTASI 10 Okt 2026** (ruang lingkup terkontrol, disetujui Khusna)

## Prioritas 1 — Analisis Kegagalan Glossary (4/40 slot)

Sumber: `~/workspace/exp_ctx/eval_unblinded.json` (arm B & D, glossary aktif).

## Kasus per kasus

| # | Sampel | Output gagal | Entri glossary | Verdict |
|---|---|---|---|---|
| 1 | S09#9 (B): "…the Hunter asked" | "tanya **Hunter itu**" | Hunter→Pemburu | **Ketidakpatuhan model** |
| 2 | S10 (B & D): "In the Northern Frontier…" | "Di **Northern Frontier**…" | Frontier→Frontier | **Kelemahan evaluasi** |
| 3 | S11 (D): "the Vampire Hunter D" | "**Vampire Hunter D**" | Vampire Hunter→Pemburu Vampir | **Ketidakpatuhan model** |
| 4 | S21 (B): "following the Hunter" | "mengikuti **sang Hunter**" | Hunter→Pemburu | **Ketidakpatuhan model** |

Catatan: "the Indiscernible Twin" yang dibiarkan di S11(D) BUKAN entri glossary —
bukan kegagalan glossary (pelajaran: nama julukan perlu dimasukkan user).

## Penyebab (bukan semua salah model)

- **Kegagalan pencocokan: 0.** Regex word-boundary terbukti cocok di semua kasus
  ("kaum Nobility" di kalimat yang sama membuktikan glossary terinjeksi).
- **Konflik terminologi: 0.**
- **Ketidakpatuhan model: 3** (kasus 1, 3, 4). Glossary ada di request, model
  mengabaikan untuk "Hunter"/"Vampire Hunter".
- **Kelemahan evaluasi: 1** (kasus 2). "Northern Frontier" yang dipertahankan
  justru KONSISTEN dengan semangat "Frontier"→"Frontier" (nama tempat dipertahankan).
  Rubrik terlalu ketat.

## Solusi minimal

1. Ketidakpatuhan model → **validasi pasca-terjemahan** (lapor, tanpa retry otomatis
   di v0). Tidak menyentuh prompt global / PROMPT_VERSION / cache. Residual ~7.5%
   diterima sebagai keterbatasan yang didokumentasikan (vs 30% tanpa glossary).
2. Kelemahan evaluasi → glossary v0 dukung **istilah multi-kata** +
   **deteksi overlap** ("Frontier" vs "Northern Frontier" → peringatan, bukan error).
3. Tidak ada perubahan pada regex pencocokan (terbukti benar).

## Yang dibangun (v0)

**File baru:**
- `novel_translator/book_memory.py` — store JSON per buku (`~/.novel_translator/book_memory/`),
  `compute_book_id` (stabil dari konten sumber), `GlossaryTerm` (+approved),
  `CharacterNote` (manual/opsional), `detect_conflicts` (duplikat/overlap/bentrokan),
  `units_affected_by_change` (unit terdampak saat glossary berubah),
  `validate_glossary` (validasi pasca-terjemahan, lapor tanpa retry),
  `export_glossary_file` (format kompatibel `--glossary`), CLI
  `python -m novel_translator.book_memory` (bookid/export/import/validate/affected/list).
- `tests/test_book_memory.py` — 11 tes (28/28 total hijau).

**Diubah:** `app.py` — satu expander "Book Memory v0" (5 tab: Glosarium, Konflik &
Dampak, Karakter, Impor/Ekspor, Validasi). Tidak mengubah konstruksi
TranslationOptions, alur job, atau widget lain.

**Tidak disentuh:** pipeline, engine, prompt, cache, checkpoint, storage,
token_budget, models, translation_skills. Skill OFF byte-identik.

## Keamanan cache (P2)

- Glossary sudah bagian dari settings → job_id/cache namespace. Perubahan glossary
  otomatis memakai namespace baru — tidak ada pemakaian cache inkompatibel.
- `units_affected_by_change`: bandingkan glossary lama vs baru, kembalikan ID unit
  yang sumbernya cocok dengan istilah tambah/hapus/ubah → dasar keputusan
  retranslate yang aman (tanpa menghapus cache lama).

## Hasil benchmark (P5)

Live test `~/workspace/exp_ctx/live_bmv0.py`:
1. dict v0 == dict manual: OK
2. settings canonical identik (job_id sama): OK
3. body request byte-identik: OK
4. live 5 unit: 2 request, "manik" dipakai benar
5. run kedua: 0 request (cache penuh): OK
Kesimpulan jujur: penyimpanan baru TIDAK mengubah instruksi model — manfaat
kualitas berasal dari glossary itu sendiri (terbukti di eksperimen A/B/C/D),
bukan dari Book Memory sebagai penyimpanan.

## Keterbatasan yang belum teratasi

1. Ketidakpatuhan model residual (~7.5% slot): model kadang mengabaikan entri
   glossary ("Hunter"→"Pemburu"). Validasi hanya melapor (v0); retry otomatis
   ditunda (butuh keputusan biaya/manfaat).
2. Validasi pasca-terjemahan memakai heuristik substring case-insensitive —
   bisa false positive pada morfologi kompleks.
3. Character notes: tanpa benchmark, tanpa klaim kualitas (sesuai instruksi).
   Disiplin anti-spoiler di tangan user (field `known_from` membantu).
4. Narrative Context penuh tetap ditunda; Genre Profiles tetap OFF default.

## Integration test (10 Okt 2026, sore) — HASIL

Ruang: `~/workspace/integ_bmv0/` (terisolasi; cache pengguna tidak tersentuh).
Buku uji: VHD7 bab 11-12 (189 unit), milik Khusna, sah untuk pengujian.

### T1 persistence — 7/7
Restart (instance baru) memuat glossary identik; buku A/B terisolasi; export→import
round-trip identik; duplikat & overlap terdeteksi; book_id stabil untuk konten sama
dan berubah bila satu paragraf diubah.

### T2 modifikasi glossary mid-project — 6/6
Bab 11 diterjemahkan penuh (107/107, 3 request) dengan glossary v1. Setelah
'Hunter': 'Pemburu'→'Sang Pemburu': units_affected_by_change = 3 unit, persis sama
dengan pencocokan regex independen (FP=0). Cache lama byte-identik (tanpa
retranslate otomatis). Validasi menemukan 2 pelanggaran riil ("Hunter" tak
diterjemahkan di u47/u106) dari 107 unit.

**Batasan (tanpa perubahan arsitektur):** pipeline tidak mendukung selective
retranslation per unit. Opsi aman saat ini: (a) daftar affected-units sebagai dasar
keputusan; (b) `--fresh` untuk retranslate penuh (boros tapi aman); (c) manual.
Tidak ada penghapusan cache otomatis.

### T3 character notes — 5/5
Simpan/panggil OK; `known_from='bab 12'` TIDAK bocor ke blok bab 2, tersedia di
bab 12; batas token bekerja; UI tidak mengklaim peningkatan kualitas.

### T4 compliance validator — 11/11 (+2 heuristik terdokumentasi)
3 regression case eksperimen terdeteksi semua (setelah perbaikan dedup span:
"Vampire Hunter" tidak lagi dilaporkan ganda dengan "Hunter"). 4 uji no-FP
(kapitalisasi, multi-kata) lolos; 2 uji no-FN lolos. Heuristik substring
didokumentasikan: 'manik-manik' lolos untuk 'manik'; glossary overbroad
('Hunter' untuk pemburu umum) adalah tanggung jawab user.

### T5 production safety
31/31 pytest hijau; EPUB output CRC valid; ENGINE_VERSION=2.0.0 & PROMPT_VERSION=2
tidak berubah; file yang diubah hanya `book_memory.py` (baru), `app.py`
(expander UI), `tests/test_book_memory.py` (baru). Skill OFF tidak tersentuh.

### Bug yang ditemukan & diperbaiki selama integrasi
1. Off-by-one penjajaran validasi (unit judul) → `epub_text_blocks()`.
2. Pelaporan ganda istilah tumpang tindih → dedup span (istilah terpanjang menang).
3. `known_from` belum dienforce → `notes_for_chapter()` + filter di
   `character_block(chapter=)` + input bab di UI.

## BASELINE STABIL v0 — DITETAPKAN 10 Okt 2026

Seluruh pemeriksaan penting lolos. Book Memory v0 dinyatakan siap untuk novel
panjang. Pengembangan fitur dihentikan sementara untuk evaluasi arsitektur
berikutnya (instruksi Khusna). Narrative Context penuh & Deep Translation tetap
ditunda. Proposal selective retry HANYA sebagai eksperimen terisolasi di masa
depan bila dibutuhkan — tidak diimplementasikan sekarang.

---

## Audit Independen & Perbaikan (10 Okt 2026 sore)

Audit menemukan 6 temuan (P0–P2). Semua diverifikasi/diperbaiki minimal;
tidak ada perubahan prompt, engine, cache, atau pipeline.

### P0 — Integrasi Streamlit (3 bug nyata, semua diperbaiki)
1. **Return value `st.data_editor`**: kode memakai `st.session_state["bm_terms"]`
   (hanya delta perubahan). Diperbaiki: pakai nilai kembali editor. Smoke test
   menemukan fakta tambahan: di Streamlit 1.63, input list-of-dicts
   mengembalikan **list**, bukan DataFrame (terbukti dari source
   `convert_pandas_df_to_data_format`). Normalisasi defensif `_editor_records()`.
2. **State tercampur antar buku**: kunci widget kini memuat book_id
   (`bm_terms_<book_id>`), diverifikasi smoke test langkah 9.
3. **Dua tombol ubah session state setelah widget dibuat** ("Muat ke kolom
   Glosarium", "Salin blok ke Instruksi tambahan"): diperbaiki via `on_click`
   callback. Smoke test menemukan bug turunan: text_area pratinjau ber-key
   tidak me-refresh (nilai lama di session state menutupi argumen baru) —
   diperbaiki dengan menghapus key pada widget display-only.
4. **Smoke test WAJIB via aplikasi Streamlit asli** (AppTest, 9/9 lolos):
   upload EPUB, render expander, on_click isi kolom glosarium, pratinjau
   anti-spoiler per bab, blok tersalin berlabel batas bab, isolasi kunci.

### P1 — Alignment validasi (temuan TERBUKTI, diperbaiki struktural)
- Kasus `e2e_rich.epub`: EpubBook = 9 unit vs `epub_text_blocks()` (regex) = 8 blok
  (unit `u3` 'village chronicle' dari struktur non-`<p>` tak tertangkap regex).
  **Tidak** diperbaiki dengan geser indeks manual.
- Solusi: `validate_epub_pair()` memakai **EpubBook di kedua sisi**; penjajaran
  = identitas struktural terverifikasi (jumlah unit + urutan dokumen).
  Bila tak dapat dipastikan → status **"unverifiable"**, bukan "konsisten".
- Temuan turunan saat verifikasi data nyata: output pipeline bertanda
  `xml:lang="id"` sehingga EpubBook menolak ekstrak (0 unit). Diperbaiki dengan
  normalisasi salinan sementara (hapus atribut lang; struktur tak berubah).
- **Occurrence counting**: kasus "Hunter met Hunter" → "Sang Pemburu bertemu
  Hunter" kini tertangkap (sumber 2x vs padanan 1x). Batasan dokumentasi:
  substitusi pronomina sah dapat tertandai (lapor saja, bukan vonis).
- Bukti data nyata (VHD7 bab 11, 107 unit): status=verified, 3 pelanggaran riil
  (u18 Frontier→Perbatasan; u47 & u106 Hunter tak diterjemahkan) — cocok dengan
  temuan T2 sebelumnya.

### P1 — Character Notes safety
- `known_from` kosong → selalu ikut; tak terurai → ikut + warning eksplisit
  (tidak diklaim sebagai perlindungan penuh).
- **Pemisahan keamanan pratinjau vs injeksi**: pratinjau disaring per bab,
  tetapi kolom Instruksi tambahan bersifat GLOBAL. UI kini menampilkan warning
  eksplisit + blok tersalin berlabel "[aman untuk bab ≤ N]". Pipeline TIDAK
  dirombak; fitur tetap referensi manual sesuai arahan audit.

### P2 — Approved status tracking
- `changed_terms()` kini membandingkan (terjemahan, approved); flip ON/OFF
  dihitung sebagai perubahan efektif → `units_affected_by_change()` ikut.
  Regression test ditambahkan.

### P2 — Portable testing
- Fixture `tests/fixtures/mini_book.epub` (1 KB) menggantikan path absolut
  `/home/hatch/...`; tidak ada lagi path absolut di test. Pytest lolos di
  environment bersih (`env -i`).

### Hasil akhir
- **38/38 pytest hijau** (21 book_memory + 12 skills + 5 format_alignment)
- Streamlit smoke test: **9/9** via aplikasi asli
- Isolasi A/B, transfer glossary, alignment 107 unit: OK
- ENGINE_VERSION=2.0.0, PROMPT_VERSION=2: tidak berubah
