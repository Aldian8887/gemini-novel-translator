# BOOK MEMORY v1 — LAPORAN IMPLEMENTASI & PENGUJIAN

Tanggal: 10 Okt 2026. Status: **Release Candidate** (menunggu audit independen).
Kode di `~/workspace/work241/`. Versi aplikasi tetap v2.4.1.

## Arsitektur

```
Chapter N (workers paralel)
   → terjemahan dengan glossary = manual + auto-approved bab < N
   → ★ PUBLICATION BARRIER (koordinator, deterministik) ★
   → AI Discovery (1 req/bab) → Local Validation R1–R6 (gratis)
   → AI Verification (batch 5/req, opsional) → Approval Router
   → persist ke Book Memory → Chapter N+1
```

## Perubahan kode

1. **`book_memory.py`** (ekstensi kompatibel): `GlossaryTerm` + status/origin/
   source_chapter/evidence/history; `BookMemory` + translation_progress,
   reader_progress, rejected_terms, discovery_log, api_usage; metode
   `glossary_upto_chapter()`, `pending_terms()`, `visible_terms()`.
2. **`glossary_auto.py`** (baru): `GlossaryAIClient`, `LocalValidator` R1–R6,
   `ApprovalRouter` (auto_safe/review_first), `run_chapter_discovery()`,
   `group_chapters()`, `chapter_job_id()`.
3. **`models.py`**: `TranslationOptions` + auto_glossary/auto_verify/
   approval_policy (default OFF; hanya masuk cache identity bila aktif —
   settings/job_id lama byte-identik).
4. **`pipeline.py`**: work loop diekstrak ke `drain()`; mode v1 menjalankan
   loop kronologis per bab + barrier. Jalur legacy tidak berubah perilaku.
   Cache per bab (`chapter_job_id`) — isolasi deterministik, resume aman.
5. **`app.py`**: tab "Otomatis v1" (toggles, policy, reader progress,
   statistik, auto-approved, pending review approve/reject, konflik);
   tab Glosarium menjadi spoiler-safe.

## Kebijakan approval

- **Auto Safe** (opt-in): local bersih + AI verified + kategori person/place
  + tanpa konflik approved + tanpa saran koreksi. Manual approved selalu menang.
- **Review First** (default): semua kandidat menunggu user.
- Koreksi AI tidak pernah diterapkan otomatis (hanya saran).
- Istilah yang ditolak user tidak diusulkan ulang otomatis.

## Hasil pengujian

- **pytest: 55/55** (38 legacy + 13 unit v1 + 4 E2E kronologis).
- **E2E kronologis** (3 bab, fake translator/AI): glossary bertambah per bab;
  bab 2 memakai istilah bab 1; konflik "Perbatasan" ditahan; manual menang;
  cancel/resume tanpa duplikasi; jalur legacy 1 translator (tak berubah).
- **Live test** (2 bab VHD nyata, Gemini API): EPUB valid; 11 istilah
  (7 auto-approved, 4 pending); konsistensi terminologi 52/52 unit, 0 violation.
- **Biaya aktual**: discovery + verifikasi ≈ 3.5K token + 2–3 request/bab;
  total ≈ 7–13K token/bab (sesuai estimasi eksperimen).

## Dampak throughput publication barrier

Barrier menambah 1 discovery + ~1–3 verification request per bab (±15–30 detik,
tergantung ukuran bab). Worker tetap paralel di dalam bab; antar-bab sekuensial
(dipersyaratkan untuk determinisme kronologis). Pada workload yang dibatasi
rate-limit API, dampak praktis minimal.

## Keterbatasan yang diakui

1. Verifier = model kecil yang sama dengan extractor (self-verification bias
   belum diuji dengan model berbeda).
2. R7 konservatif: proper noun yang diterjemahkan selalu ke review manusia.
   Ini disengaja (keputusan pengguna), bukan overfit — tetapi berarti
   terjemahan nama yang sah ("Ibu Kota") pun menunggu manusia.
3. Discovery dibatasi 60K karakter/bab; bab sangat panjang terpotong.
4. Auto Safe hanya untuk person/place; kategori lain selalu butuh manusia.
5. Spoiler-safe adalah filter tampilan; bukan keamanan absolut bila user
   membuka seluruh data.
6. Validator unit-level terbatas untuk nama satu huruf ("D" → pronomina
   wajar tertandai; ini batas validator, bukan bug produk).
7. Belum ada benchmark kualitas vs Default + Manual Glossary (pekerjaan berikut).

## Verifikasi Release Candidate (10 Okt 2026)

Uji 5 bab VHD7 berurutan (743 unit) via pipeline nyata + API Gemini:

- Propagasi: 26/26 istilah relevan dikirim via API ke bab berikutnya.
- Compliance istilah multi-karakter: 100%.
- R7 menahan Frontier→Perbatasan, Capital→Ibu Kota, Vampire Hunter D,
  Cyrus's Curio Shop untuk review; 20 istilah auto-approved, 0 konflik.
- Resume setelah 503: mulus, 0 duplikasi.
- Spoiler: reader_progress=2/5 → 0 kebocoran.
- Biaya: ±13K token/bab (discovery+verifikasi).

Detail: `BENCHMARK_BOOK_MEMORY_V1_RC.md`.

## Hotfix Audit Independen (10 Okt 2026)

Audit menemukan 5 regresi; semuanya diperbaiki minimal:

1. **P0 — Publication progress vs cache job**: `translation_progress`
   (book-global) tidak lagi dipakai untuk skip bab. Kelengkapan ditentukan
   oleh cache per-job; ganti style/config dan `ignore_cache=True` kini
   menerjemahkan ulang dengan benar.
2. **P0 — Manual menang**: `effective_chapter_glossary()` — memory dulu,
   manual menimpa, termasuk normalisasi kapitalisasi.
3. **P0 — R8**: istilah sumber harus muncul di teks bab; "Zorba" karangan
   tidak bisa Auto Safe meski AI bilang verified.
4. **P1 — Cache identity**: `chapter_job_id()` kini mencakup hash glossary
   efektif; perubahan glossary → namespace baru (lama dipertahankan,
   tidak dihapus).
5. **P1 — Token aktual**: `complete_json()` memanggil
   `limiter.usage(reservation, in_tok, out_tok)` + `success=True`.
6. **P2 — Spoiler fail-closed**: `visible_terms()` default tertutup
   (hanya bab 1) bila progres belum diatur; "tampil semua" harus eksplisit.

Pengujian: 63/63 (55 bawaan + 5 audit + 3 baru).

## Hotfix Audit RC2 (10 Okt 2026)

1. **P1 — Evidence Binding**: R8 diperketat — istilah sumber HARUS muncul
   dalam `supporting_text`-nya sendiri (bukan sekadar di mana pun di bab).
   Bukti "tempel" kini ditahan untuk review.
2. **P1 — Manual di Discovery**: `run_chapter_discovery()` menerima
   `manual_glossary`; digabung ke approved untuk R5 (manual menang) dan ke
   canonical verifier. Konflik Toto→Toto vs manual Toto→Nama Manual kini
   pending, tidak tersimpan sebagai auto-approved.
3. **P1 — Reader Progress 999**: sentinel 999 dihapus. Kontrol "Tampilkan
   semua (berisiko spoiler)" terpisah; angka progres selalu berarti nomor
   bab sebenarnya.
4. **P2 — Token Accounting**: `limiter.usage()` tidak lagi `except: pass`
   diam-diam — kegagalan dilog via status callback + `_usage_errors`.
   Usage direkonsiliasi SEBELUM parsing JSON (HTTP 200 = sudah ditagih).

Pengujian: 65/65 (63 + 2 adversarial baru).

## Hotfix Ekspor Spoiler-Safe (10 Okt 2026)

Audit RC3 menemukan: tombol Unduh Glosarium mengekspor seluruh approved
glossary termasuk bab mendatang, meski Reader Progress membatasi tampilan.

- Fungsi baru `spoiler_safe_glossary_text()`: hanya istilah approved yang
  diizinkan `visible_terms()`.
- UI default memakai ekspor aman; "Ekspor lengkap (berisiko spoiler)" via
  checkbox eksplisit.
- `export_glossary_file()` TIDAK berubah — CLI/API tetap ekspor penuh.

Pengujian: 66/66. **Book Memory v1 dibekukan sebagai stable baseline.**
