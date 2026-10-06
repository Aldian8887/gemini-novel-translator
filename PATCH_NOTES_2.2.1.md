# Patch Notes 2.2.1 — Diagnostic failure log

Tanggal: 2 Oktober 2026. Patch diagnostik kecil di atas 2.2.0; **tidak mengubah
perilaku terjemahan, packing, rotasi key, atau format cache.**

## Kenapa patch ini ada
Pada pengujian 2.2.0, rasio Sukses : Retry teramati hampir 1:1 dengan
`429: 0` — artinya retry berasal dari batch yang gagal validasi lalu dibelah
(split recovery), bukan dari kuota. Dashboard tidak menampilkan *jenis*
kegagalan validasinya, sehingga penyebab pastinya belum bisa disimpulkan.
Patch ini mencatat setiap kegagalan ke file log agar bisa dianalisis.

## Yang berubah
- Modul baru `novel_translator/diagnostics.py`: `FailureLogger` — pencatat
  JSONL yang thread-safe (aman untuk multi-worker).
- `pipeline.py`: setiap batch yang gagal validasi dicatat (`invalid_response`)
  atau butuh packing ulang (`repack`), beserta pesan error persisnya, jumlah
  unit/run, estimasi token input, dan contoh ID.
- `gemini_client.py`: setiap retry level HTTP dicatat (`http_retry` dengan kode
  HTTP, `timeout`, `transport_error`).
- File log: `<workspace>/diagnostics/failures.jsonl` — satu baris JSON per
  kegagalan. Contoh baris:

  ```json
  {"ts": "2026-10-02T13:05:11", "job": "a1b2c3d4", "title": "Designing Your Life",
   "kind": "invalid_response", "units": 12, "runs": 96, "est_input_tokens": 16210,
   "error": "ID teks hilang, duplikat, atau tidak dikenal.",
   "sample_ids": ["p000123", "p000124", "p000125"]}
  ```

## Privasi
Log hanya berisi pesan error, ukuran batch, dan ID potongan teks — **tidak**
berisi isi respons, isi request, atau API key. Aman dibagikan untuk debugging.

## Cara pakai
1. Pasang seperti update biasa (timpa file program, pertahankan `workspace`).
2. Jalankan terjemahan 10–15 menit seperti biasa.
3. Buka `workspace/diagnostics/failures.jsonl` (di samping folder `cache`),
   kirim isinya untuk dianalisis.

## Kompatibilitas
- `ENGINE_VERSION` tetap 2.0.0 / `PROMPT_VERSION` 2 — cache lama tetap dipakai.
- Tidak ada kolom database baru; tidak ada perubahan perilaku retry/split.
- Setelah penyebab ditemukan dan diperbaiki, patch diagnostik ini boleh
  dilepas kembali tanpa efek samping.
