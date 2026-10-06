# Patch Notes v2.2.4 — Salvage Parsial (respons tidak sempurna tidak lagi dibuang)

## Latar belakang

Bukti live v2.2.2/v2.2.3 menunjukkan kegagalan batch nyaris selalu berupa
respons yang *hampir* benar: ter-parse, tetapi 1–3 ID dari 100+ unit hilang/
duplikat/meleset. Strategi lama membuang 100% isi batch lalu mengulang dari
nol (dibalik/dibelah), sehingga satu ID yang selip berharga satu batch penuh.

v2.2.4 menyerang tepat di titik itu: **potongan yang valid dari respons yang
tidak sempurna langsung disimpan; hanya potongan rusak/hilang yang diminta
ulang** dalam satu batch susulan kecil.

## Yang berubah

- `models.py`: fungsi baru `validate_response_partial()` — menilai setiap
  run satu per satu (ID unit/run hilang, duplikat, skema, teks kosong,
  terlalu pendek/panjang, karakter XML). Mengembalikan
  (terjemahan_valid, id_run_rusak, daftar_alasan).
- `gemini_client.py`: `translate_batch_partial()` — jalur request yang sama
  (HTTP retry, rotasi key, cooldown tidak berubah), tetapi hasil validasi
  dikembalikan sebagai (valid, rusak, alasan). Mode ketat lama dipertahankan
  persis untuk pemanggil lain. Pemeriksaan markup HTML kini per potongan:
  run yang ketambahan tag menjadi rusak, bukan membatalkan batch.
- `pipeline.py`: hasil batch kini (valid, unit_rusak, alasan). Potongan
  valid di-commit segera; sisa rusak menjadi satu batch susulan di depan
  antrean pemulihan. Bila **nol** potongan terselamatkan, perilaku lama
  berlaku utuh: balik urutan sekali → belah → gagal pada unit tunggal.
  Setiap putaran yang menyimpan ≥1 potongan adalah progres, jadi loop
  dijamin berhenti.
- `diagnostics.py`: catatan `salvage` baru di `failures.jsonl` berisi
  jumlah run terselamatkan (`salvaged_runs`) vs total batch (`batch_runs`)
  dan ukuran susulan — tanpa isi respons/teks.

## Yang TIDAK berubah

- Prompt, skema respons, ID unit/run: identik.
  `ENGINE_VERSION=2.0.0`, `PROMPT_VERSION=2` — cache 2.0.x–2.2.3 kompatibel
  penuh, termasuk 40.899 blok Deep Sea Embers dan progres buku berjalan.
- Packing budget-output, rotasi multi-key, akuntansi RPD/RPM/TPM, jeda/
  resume, dan pemeriksaan integritas EPUB: identik.

## Bukti live (2 Okt 2026, dua API key asli)

Buku uji identik untuk semua konfigurasi (431 unit / 671 potongan, paragraf
panjang ala novel; byte EPUB sama):

| Konfigurasi | Request | Invalid | Salvage | Waktu |
|---|---|---|---|---|
| v2.2.3 (baseline) | 10 | 3 | — | 526 dtk* |
| v2.2.4 salvage | **7** | **0** | 1× (236/311 run selamat) | 263 dtk |
| v2.2.4 + batch separuh | 14 | 0 | 6× (612 run selamat) | 277 dtk |
| v2.2.4 + temperature 0 | 7 | 0 | 2× (417 run selamat) | 212 dtk |

\* waktu baseline tercemar backoff HTTP 503 yang panjang; bandingkan
terutama kolom Request.

Kesimpulan terukur:
- Salvage menurunkan request ~30% pada konfigurasi bawaan dan menghapus
  seluruh kegagalan `invalid_response` dari log (respons tidak sempurna
  bukan lagi "kegagalan" — ia menjadi progres + susulan kecil).
- Batch separuh justru menambah request (dasar request berlipat, fidelitas
  per percobaan tidak naik terukur) → pengaturan bawaan dipertahankan.
- Temperature 0 setara 0,2 → tidak diubah.
- Satu kejadian ekstrem yang teramati: batch 150 run terselamatkan 149 —
  satu ID yang selip kini berharga satu request susulan berisi 1 potongan,
  bukan pengulangan 150 potongan.

## Cara membaca hasil di pemakaian nyata

- Dashboard: counter Retry kini mencakup batch susulan; itu wajar.
- `failures.jsonl`: baris `"kind": "salvage"` adalah kejadian respons tidak
  sempurna yang berhasil diselamatkan (bukan kegagalan). Baris
  `invalid_response` kini hanya muncul bila satu batch utuh tidak
  menyisakan satu potongan valid pun.
