# BENCHMARK — Book Memory v1 Release Candidate

Tanggal: 10 Okt 2026. Metode: 5 bab berurutan VHD7 (743 unit) via
`translate_novel` asli + Gemini API nyata. R7 aktif.

## Ringkasan eksekusi

- 5 bab / 787 potongan teks (termasuk judul) / 743 unit novel.
- Waktu: 25,4 menit (termasuk 1x 503 transient → retry otomatis → resume mulus).
- Istilah: 48 (20 auto-approved, 27 pending review, 1 manual).
- Biaya discovery+verifikasi: 65.490 token / 24 request (±13K token/bab).

## Propagasi terminologi (bukti)

Glossary yang diterima translator per bab: 1 → 7 → 10 → 19 → 20 istilah.
Semua istilah approved yang relevan di bab N+1 **dikirim via API** (26/26 YA):

| Bab | Istilah relevan (kemunculan) |
|-----|------------------------------|
| 2 | Toto x36, Wu-Lin x17, Cronenberg x1 |
| 3 | D x45, Wu-Lin x28, Florence x4, Professor Krolock x3, Toto x3 |
| 4 | D x44, Glen x12, Su-In x10, Shin x4, ... (10 istilah) |
| 5 | D x35, Su-In x30, Florence x5, ... (8 istilah) |

Tidak ada istilah bab mendatang yang bocor ke bab sebelumnya.

## Compliance terjemahan

- Istilah multi-karakter: **100%** (Toto 69/69, Wu-Lin 97/97, dsb.).
- Validator unit-level: 743 unit, 85 temuan —
  84 di antaranya untuk "D" satu huruf (batas validator: nama satu huruf
  wajar menjadi pronomina; bukan bug produk),
  1 artefak run-splitting pada harness uji (bukan translator asli).
- Compliance kasar keseluruhan: 80,5% (514 kemunculan relevan).

## Auto Safe + R7 (edge cases nyata)

R7 menahan untuk review (tidak auto-approved):
- Frontier → Perbatasan (proper noun diterjemahkan)
- Capital → Ibu Kota
- Vampire Hunter D → Pemburu Vampir D
- Cyrus's Curio Shop → Toko Barang Antik Cyrus

Tetap auto-approved (dipertahankan): Toto, Wu-Lin, Cronenberg, D,
Florence, Belhistan, Professor Krolock→Profesor Krolock (gelar baku),
Glen, Peres, Gilligan, Shin, Su-In, dsb. — 20 istilah, 0 konflik.

## Kronologi / cache / resume

- Publication barrier deterministik dengan workers=2.
- Cancel (503) → resume: cache per-bab dipakai ulang, tanpa duplikasi
  istilah auto (0 duplikat dari pipeline).
- Snapshot glossary per bab cocok dengan request (tercatat di translation log).
- Manual "Nobility"→"Kaum Ningrat" dihormati; kandidat "Nobles"→"Para Bangsawan"
  ditahan (R5).

## Spoiler safety

- reader_progress=2 dari 5 bab: 24 istilah terlihat (bab 1–2),
  11 istilah bab 3–5 disembunyikan, **0 kebocoran**.
- Batasan: filter tampilan, bukan keamanan absolut (jalur manual/export
  seluruh data tetap dapat menampilkan).
