# Patch Notes 2.2.3 — retry urutan unit dibalik

Tanggal: 2 Oktober 2026. Koreksi strategi retry berdasarkan bukti log live.

## Kenapa diubah
v2.2.2 memperkenalkan pengulangan batch *identik* sekali untuk kesalahan
mekanis. Log diagnostik sesi live "Vampire Hunter D Vol 08" (18:02–18:12)
membuktikan strategi itu tidak efektif di dunia nyata:

- 22 kegagalan, semuanya `invalid_response` mekanis (ID teks/unit hilang,
  jumlah tidak cocok). 0× 429, 0× MAX_TOKENS.
- **8 pasang batch identik gagal dua kali berturut-turut** — retry identik
  terpicu, tapi 8/8 gagal lagi. Di temperature 0,2 model nyaris
  deterministik: input sama persis menghasilkan kesalahan yang sama.
- Akibatnya rasio Retry : Sukses tetap ~1:1 (19 : 14 di dashboard).

Backtest offline 2.2.2 sebenarnya sudah memodelkan rezim ini ("sticky",
+32% lebih boros); data live memastikan model Gemini masuk rezim tersebut.

## Yang berubah (satu-satunya perubahan perilaku)
- `pipeline.py`: pengulangan untuk kesalahan mekanis kini mengirim batch
  yang sama dengan **urutan unit dibalik**. Isi unit, ID, validasi, dan
  jaminan cache tidak berubah — hanya urutan kirim. Tujuannya memecah
  determinisme model (setiap posisi berubah) tanpa biaya tambahan.
- Gagal lagi setelah dibalik → dibelah seperti biasa. Kesalahan
  deterministik (MAX_TOKENS, terlalu pendek, RepackRequired) tetap
  langsung dibelah tanpa pengulangan.
- Penanda `retried` kini berbasis himpunan ID unit (bukan identitas
  objek), sehingga satu konten batch hanya mendapat satu jatah
  pengulangan walau urutannya berubah.
- Selain itu identik dengan 2.2.2 (packing, rotasi multi-key, log
  diagnostik tetap aktif).

Vonisnya dibaca dari sesi live berikutnya: bila pengulangan-dibalik mulai
berhasil, pasangan kegagalan identik akan hilang dari
`workspace/diagnostics/failures.jsonl` dan rasio Sukses : Retry membaik.

## Kompatibilitas
- `ENGINE_VERSION` 2.0.0 / `PROMPT_VERSION` 2 tidak berubah: cache lama
  tetap dipakai.
- Kembali ke 2.2.1/2.2.2 selalu aman; tidak ada migrasi data.
