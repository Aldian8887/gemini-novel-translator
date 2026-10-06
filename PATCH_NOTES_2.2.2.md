# Patch Notes 2.2.2 — same-batch retry untuk kesalahan mekanis

Tanggal: 2 Oktober 2026. Perbaikan perilaku retry berdasarkan analisis log
diagnostik 2.2.1 dan backtest offline (60 run, fault-injection).

## Temuan dari log diagnostik (2.2.1)
Dari 39 kegagalan dalam ±16 menit sesi "Vampire Hunter D Vol. 7":
- **35× `invalid_response`**, 4× HTTP 503, **0× 429, 0× MAX_TOKENS, 0× repack**.
- Penyebab dominan (31 dari 35): **kesalahan mekanis protokol** — ID teks/unit
  hilang/duplikat, jumlah unit/potongan tidak cocok, skema salah, markup HTML,
  teks kosong. Bukan kuota, bukan kepotong output, bukan packing.
- Pola cascade: satu region konten bisa gagal 4× berturut-turut sambil
  dibelah (370 → 189 → 86 → 42 unit); tiap level membakar 1 request.

## Yang berubah
- `errors.py`: `InvalidResponse` kini membawa penanda `same_batch_retry`.
- `models.py` / `gemini_client.py`: kesalahan mekanis yang stokastik
  (ID/jumlah/skema tidak cocok, teks kosong, markup HTML) ditandai
  `same_batch_retry=True`. Kesalahan deterministik (MAX_TOKENS, respons
  terlalu pendek, karakter XML ilegal, `RepackRequired`) **tidak** ditandai.
- `pipeline.py`: batch yang gagal dengan kesalahan mekanis **dicoba ulang
  identik SEKALI** sebelum dibelah. Kesalahan lain langsung dibelah
  seperti sebelumnya. Semua jaminan keselamatan tetap berlaku: validasi
  tetap ketat, split tetap fallback, cache/ID tidak berubah.
- Counter dashboard `Retry` kini juga menghitung pengulangan batch ini,
  sehingga rasio Sukses : Retry tetap dapat dibandingkan seperti sebelumnya.

Hasil yang diharapkan: episode kegagalan yang sebelumnya membakar 3–6
request (split cascade) sering selesai dalam 2 request (gagal + ulang
berhasil), dan rasio retry:sukses membaik. Bonus: batch/run tunggal yang
gagal sesekali kini punya kesempatan kedua (di 2.2.1 job bisa halt total
bila satu run tunggal gagal).

## Hasil backtest offline (fault-injection, buku sintetis 1.318 run)
v2.2.1 vs v2.2.2, 4 skenario × 5 seed, pipeline nyata + limiter nyata
(pacing divirtualisasi; request palsu deterministik):

| Skenario | Request (rata-rata) | Retry | Catatan |
|---|---|---|---|
| Stokastik p=0,30 | 15,6 → **10,2** (−35%) | 4,8 → 3,6 | |
| Stokastik p=0,45 | 21,8 → **14,0** (−36%) | 8,2 → 6,2 | v2.2.1 **mati total 1 dari 5 run**; v2.2.2 selesai 5/5 |
| Bergantung ukuran | 13,6 → **11,6** (−15%) | 3,8 → 4,2 | |
| Sistematis ("sticky", retry identik gagal lagi 85%) | 17,6 → 23,2 (**+32%**) | 5,8 → 11,8 | Skenario adversarial; satu-satunya rezim di mana v2.2.2 lebih boros |

- Teks output terbukti **identik 19/19 pasangan** yang selesai-dua-duanya:
  perubahan ini hanya menyentuh efisiensi, tidak pernah hasil terjemahan.
- Penajaman tambahan "retry hanya untuk batch ≥16 run" diuji dan
  **ditolak**: tidak menolong skenario sticky dan justru menghilangkan
  jaring pengaman run tunggal.

Keterbatasan backtest: model kesalahan disimulasikan. Dunia nyata berada
di antara stokastik dan sticky; vonis akhir tetap dashboard sesi nyata
pertamamu (bandingkan Sukses : Retry). Bila rasio memburuk konsisten,
kembali ke v2.2.1 selalu aman (cache kompatibel).

## Kompatibilitas
- `ENGINE_VERSION` 2.0.0 / `PROMPT_VERSION` 2 tidak berubah: cache lama
  (termasuk 40.899 blok Deep Sea Embers) tetap dipakai.
- Log diagnostik 2.2.1 (`workspace/diagnostics/failures.jsonl`) tetap aktif.
