# Terobosan 2.2.0 — packing budget-output & rotasi multi-key

Dua perubahan utama, keduanya teruji offline dan live:

## 1. Packing berbasis budget output (bukan target input tetap)

`TokenPacker.take()` kini menghitung target input dinamis dari 90% budget
output memakai rasio ekspansi adaptif (`output_driven_target()`), lalu
`fits()` — yang mengukur overhead JSON sebenarnya — tetap menjadi penentu
akhir. Target manual (`target_input_tokens`) tetap menjadi rel pengaman.

Default baru: Aggressive 30K/36K (dari 14K/18K), Balanced 24K/30K,
Safe tetap 14K/18K sebagai opsi konservatif. Batas validasi tetap 40K.

Pengukuran live (Gemini 3.5 Flash Lite, EN→ID): rasio output/input nyata
1,44 — di bawah asumsi awal 1,65. Pada novel uji, jumlah request turun
dari 8 menjadi 4 untuk teks yang sama (~2x teks per RPD).

## 2. Rotasi API key otomatis

Kuota Google berlaku per project, bukan per key. Tool kini menerima banyak
key (`TranslationOptions(api_keys=[...])`, CLI `--api-key` dapat diulang,
env `GEMINI_API_KEYS`, UI Streamlit: key utama + expander key tambahan).

- Akuntansi RPD/RPM/TPM dan cooldown per key: scope DB
  `{model}:{digest(key)[:16]}`. Key asli tidak pernah masuk database.
- `reserve()` memilih key berkuota round-robin dan mem-pin key pada request,
  sehingga rotasi antar worker tidak menukar key di tengah jalan.
- 429 harian/nol pada satu key → `exhaust_key()` menandai key itu habis lalu
  `KeyExhausted` memicu percobaan ulang batch yang sama dengan key
  berikutnya, tanpa memakan budget `max_retries`. Hanya bila semua key habis,
  sesi dijeda seperti dulu.
- UI: RPD accounting dapat memilih key mana yang dikoreksi/direset;
  panel metrik menampilkan key aktif dan jumlah key berkuota.

## Kompatibilitas

- ENGINE_VERSION=2.0.0 dan PROMPT_VERSION=2 tidak berubah: seluruh cache
  terjemahan 2.0.x/2.1.x (termasuk 40.899 blok) tetap dapat dipakai penuh.
- Mode satu key tidak berubah perilakunya: `api_key` lama tetap didukung
  sebagai fallback; `--offline`/`--inspect` tetap jalan tanpa key.
- SATU PERUBAHAN TERLIHAT: counter RPD lokal kini per key. Counter lama
  (scope model saja) menjadi yatim dan tidak terbaca lagi; ini hanya
  penghitung pengaman lokal — batas asli tetap ditegakkan Google via 429
  dan ditangani otomatis. Riwayat RPM/TPM 60 menit terakhir juga per key.

## Pengujian

- `py_compile` seluruh 16 file: OK.
- Unit test rotasi offline (fake keys): round-robin AAA,AAA,BBB,BBB,CCC,CCC;
  Paused saat semua habis; `exhaust_key`; kompatibilitas satu key;
  isolasi scope digest — semua lolos.
- Pipeline offline (fake translator): 27/27 dan 194K karakter, resume dari
  cache: OK. Packing baru: 4 batch vs 8 batch untuk teks yang sama.
- Live dengan API key asli (free tier): terjemahan Indonesia nyata masuk
  EPUB dan lolos pemeriksaan integritas; exhaustion (rpd=1) berhenti
  bersih dengan pesan yang benar; rotasi ke key ke-2 terbukti
  (batch-1 via key asli tersimpan, batch-2 mencoba key berikutnya).
  Total ±15 request selama seluruh pengujian.

## Catatan

- Ditemukan saat pengujian (bukan bug tool): `httpx==0.28.1` crash saat
  startup bila environment `no_proxy` berisi literal IPv6 seperti `[::1]`
  (kasus di VM penguji). Di Windows pengguna normal tidak terjadi.
  Pertimbangkan menaikkan versi httpx pada rilis berikut.
