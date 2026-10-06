# Changelog

## 2.4.1 — hemat token jalur gateway (OpenAI-compatible)

- Khusus provider "openai": batas output per model gateway (claude-sonnet-4-5 dan qwen-3-8-max: 32.768 token/batch), alias ID pendek u0../r0.. di lapisan transport (dipetakan kembali sebelum validasi), dan watchdog anggaran token sesi `--oa-token-budget` yang berhenti bersih sebelum anggaran terlampaui. Lihat `PATCH_NOTES_2.4.1.md`.
- Hotfix live test 5 Okt 2026 dimasukkan resmi: parse JSON toleran tanda kutip mentah pada respons model gateway.
- Jalur Gemini tidak berubah: ENGINE_VERSION=2.0.0, PROMPT_VERSION=2; settings/job_id mode Gemini byte-identik dengan 2.4.0 (terverifikasi); file mesin inti tidak disentuh.

## 2.4.0 — penyedia OpenAI-compatible (opsional)

- Penyedia kedua opt-in: gateway OpenAI-compatible (chat completions, mis. bansosai) di samping Gemini, dipilih lewat UI ("Penyedia AI") atau CLI `--provider`. Klien baru `openai_client.py` memakai prompt, validasi, salvage, packing, limiter, dan ekspor yang sama; respons gateway dinormalisasi (bungkus `{"units": [...]}`, kunci ekstra) sebelum validasi; error HTTP 200 + body `{"error": ...}` diklasifikasikan dari kodenya.
- Key gateway (`--api-key`/`OA_API_KEY`) terpisah dari key Gemini dan tidak pernah tercampur; cache hasil gateway berada di namespace sendiri (provider + base URL masuk settings hanya untuk provider non-gemini).
- Kompatibilitas: settings/job_id mode Gemini byte-identik dengan 2.3.0 (terverifikasi 5 konfigurasi); ENGINE_VERSION=2.0.0, PROMPT_VERSION=2 tidak berubah; file mesin inti byte-identik.
- Live terbukti di bansosai/`claude-sonnet-4-5`: Austen 2 request, Darwin 1 request, EPUB valid. Lihat `PATCH_NOTES_2.4.0.md`.

## 2.3.0 — domain fiksi/non-fiksi + generate ulang

- Domain terjemahan: instruksi sistem khusus non-fiksi (akurasi fakta/angka/istilah) di samping fiksi; dipilih lewat UI ("Gaya dan istilah") atau CLI `--domain`. Hasil tiap domain tersimpan di namespace cache terpisah.
- Mode generate ulang (UI checkbox / CLI `--fresh`): abaikan cache buku ini, terjemahkan dari nol, timpa output — untuk membandingkan kualitas antar domain/gaya/model.
- Kompatibilitas: default fiksi tidak menulis kunci domain ke settings(), sehingga job_id dan cache 2.0.x–2.2.4 byte-identik; ENGINE_VERSION=2.0.0, PROMPT_VERSION=2 tidak berubah.
- Live: 4 konfigurasi lintas-domain (Austen, Darwin) + demo generate-ulang terbukti; analisis kualitas di `laporan_kualitas_v2.3.0.md`. Lihat `PATCH_NOTES_2.3.0.md`.

## 2.2.4 — salvage parsial

- Respons tidak sempurna tidak lagi dibuang: `validate_response_partial()` (models) + `translate_batch_partial()` (gemini_client) menilai setiap potongan; pipeline menyimpan potongan valid dan meminta ulang hanya potongan rusak/hilang dalam batch susulan kecil. Nol terselamatkan -> jalur lama utuh (balik sekali -> belah).
- Diagnostik: kind baru `salvage` di failures.jsonl (salvaged_runs vs batch_runs).
- Prompt/skema/ID tidak berubah: ENGINE_VERSION=2.0.0, PROMPT_VERSION=2; cache 2.0.x–2.2.3 kompatibel penuh.
- Live (buku uji identik, 2 key asli): 7 request vs 10 pada v2.2.3, invalid_response 0 vs 3; batch separuh (14) dan temperature 0 (7) tidak lebih baik. Lihat `PATCH_NOTES_2.2.4.md`.

## 2.2.3 — retry urutan unit dibalik

- Koreksi strategi 2.2.2 berdasarkan log live: pengulangan batch identik terbukti gagal 8/8 (model nyaris deterministik di temperature 0,2).
- `pipeline.py`: pengulangan untuk kesalahan mekanis kini mengirim batch yang sama dengan urutan unit dibalik (isi/ID/validasi/cache tidak berubah); gagal lagi -> dibelah seperti biasa. Penanda `retried` berbasis himpunan ID unit.
- Selain itu identik dengan 2.2.2. Lihat `PATCH_NOTES_2.2.3.md`.

## 2.2.2 — same-batch retry untuk kesalahan mekanis

- Berdasarkan analisis log diagnostik 2.2.1: 35 dari 39 kegagalan adalah kesalahan mekanis protokol model (ID/jumlah/skema tidak cocok), 0× 429/MAX_TOKENS/repack.
- `InvalidResponse` kini membawa penanda `same_batch_retry`; kesalahan mekanis stokastik ditandai di `models.py`/`gemini_client.py`, kesalahan deterministik (MAX_TOKENS, terlalu pendek, XML ilegal, RepackRequired) tidak.
- `pipeline.py`: batch dengan kesalahan mekanis dicoba ulang identik SEKALI sebelum dibelah; validasi, split fallback, dan jaminan cache tidak berubah. Counter Retry dashboard mencakup pengulangan ini.
- Backtest offline (60 run fault-injection): request −35% (stokastik p=0,30), −36% (p=0,45; v2.2.1 mati total 1/5, v2.2.2 selesai 5/5), −15% (ukuran), +32% pada skenario adversarial "sticky". Output teks identik 19/19 pasangan. Lihat `PATCH_NOTES_2.2.2.md`.

## 2.2.1 — diagnostic failure log

- Patch diagnostik di atas 2.2.0; tidak mengubah perilaku terjemahan, packing, rotasi key, atau format cache.
- Modul baru `novel_translator/diagnostics.py` (`FailureLogger`, thread-safe, JSONL).
- Setiap kegagalan batch dicatat ke `<workspace>/diagnostics/failures.jsonl`: `invalid_response`/`repack` (pipeline, dengan pesan validasi persisnya) dan `http_retry`/`timeout`/`transport_error` (level HTTP). Hanya berisi pesan error, ukuran batch, dan ID potongan — tanpa isi respons/request atau API key.
- Lihat `PATCH_NOTES_2.2.1.md` untuk cara pakai.

## 2.2.0 — packing budget-output dan rotasi multi-key

- Packing kini digerakkan budget output: `TokenPacker` menghitung target input dinamis dari 90% batas output memakai rasio ekspansi adaptif; `fits()` tetap menjadi penentu akhir. Default Aggressive 30K/36K (dari 14K/18K), Balanced 24K/30K, Safe tetap 14K/18K.
- Rotasi API key otomatis: dukung banyak key (CLI `--api-key` dapat diulang, env `GEMINI_API_KEYS`, UI Streamlit). Akuntansi RPD/RPM/TPM dan cooldown per key (scope digest, key asli tidak masuk DB). 429 harian pada satu key memicu pindah ke key berikutnya tanpa memakan budget retry; jeda hanya bila semua key habis.
- ENGINE_VERSION=2.0.0 dan PROMPT_VERSION=2 tidak berubah: cache terjemahan 2.0.x/2.1.x tetap kompatibel penuh. Satu-satunya yang berubah terlihat: counter RPD lokal kini per key (counter lama menjadi yatim, tidak memengaruhi cache).
- Teruji: unit rotasi offline, pipeline offline, dan live dengan API key asli (terjemahan nyata, exhaustion bersih, rotasi antar key terbukti).

## 2.1.2 — perbaikan ekspor Windows dan pemulihan cache lengkap

- Buka file EPUB sementara dengan akses baca/tulis sebelum fsync; mode baca saja menyebabkan EBADF pada Windows.
- Tetap hentikan penerbitan output bila sinkronisasi disk benar-benar gagal; cache lengkap dapat diekspor ulang tanpa API.
- Pulihkan Deep Sea Embers dari 40.899 blok cache valid dengan nol request. Seluruh file workspace asli tetap identik.
- 16 tes terfokus lolos, termasuk simulasi akses flush Windows, pemulihan setelah kegagalan disk, sembilan pemeriksaan 2.1.1, dan perlindungan struktur/aset EPUB.
- Audit menemukan potongan yang masih sama dengan teks Inggris dan pelanggaran EPUBCheck bawaan sumber; detail di PATCH_NOTES_2.1.2.md dan validation/v2.1.2.

## 2.1.1 — patch target manual dan Local RPD

- Auto Tune default OFF, input default 14K/maksimum 18K, RPM lokal 13; pengaturan manual tetap dapat diubah.
- Abaikan target kecil tersimpan saat mode manual; split lokal tidak mengubah target batch normal berikutnya. TPM adaptif hanya mengatur scheduling.
- Perketat transaksi reservasi bersama dan timestamp setelah lock; bersihkan cooldown kedaluwarsa saat scheduling dilanjutkan.
- Tambahkan Official RPD Limit, Local RPD Safety Limit, edit Current Local RPD Used dan reset ke nol. Hitungan persisten hanya menambah generation request; tidak menghapus riwayat RPM/TPM atau progres.
- Sembilan pemeriksaan terfokus lolos pada data sementara, termasuk salinan cache aktif unggahan. Lihat PATCH_NOTES_2.1.1.md.

## 2.1.0 — 15 September 2026, Performance Mode

- Lanjutkan aplikasi 2.0.1: seluruh fungsi EPUB, cache/checkpoint, glossary, instruksi, CLI dan Streamlit dipertahankan.
- Ganti batching karakter dengan token packing seluruh prompt, lintas chapter, dengan boundary paragraf dan estimasi output.
- Cari batas paragraf utuh sebelum prefix parsial, agar hilangnya konteks duplikat tidak membuat paragraf yang sebenarnya muat ditolak.
- Kirim glossary relevan saja, ringkas systemInstruction, hilangkan konteks berulang dan tambahkan referensi konteks bersama.
- countTokens opsional, kalibrasi persisten, usageMetadata aktual, dan batas input 40K; output 65.536, thinking MINIMAL dan temperature 0.2 untuk Gemini 3.5 Flash Lite.
- ThreadPoolExecutor 1–4 worker, default 2, memakai satu ledger RPM/TPM/RPD atomik. Reservasi token diperbarui dari usage aktual; waktu tunggu mengikuti expiry jendela 60 detik.
- Profil Aggressive 15 RPM / target 240K / hard 248K / 490 RPD / input 36K. Anggaran proyek resmi 15/250K/500 divalidasi, tidak ada rotasi key/proyek.
- Adaptive batch + TPM/RPM/concurrency, 429 terklasifikasi, backoff global persisten, pemecahan respons rusak langsung dan drain hasil valid saat jeda/error/kuota harian.
- Dashboard metrik/throughput/ETA/RPD; pemulihan pengaturan checkpoint dan reconnect job setelah refresh browser.
- Cache v2.0.x tetap kompatibel. Fixture v2.0.1 asli digunakan untuk membuktikan hanya bagian yang belum selesai dikirim.
- Tes tambahan packing, quota gabungan empat thread, koreksi usage, tuning persisten, pemulihan concurrent MAX_TOKENS/RPD/cancel, dan kontrol UI. Bukti eksekusi akhir di validation/v2.1.0.

## 2.0.1 — perbaikan instalasi Python 3.14

- Perbaiki pin pandas dari 2.2.3 ke 2.3.3 agar tersedia wheel Python 3.14, termasuk Windows amd64.
- Installer hanya memakai wheel, memeriksa CPython 3.11–3.14 64-bit dengan GIL, dan menampilkan versi interpreter yang benar-benar dipakai .venv.
- Launcher Windows memilih versi Python yang didukung, lalu melanjutkan instalasi gagal tanpa membuat ulang lingkungan yang masih dapat dipakai.
- Penanda sukses baru ditulis setelah pip check dan impor dependensi berhasil. Perubahan interpreter/constraints memicu pembaruan dependensi.
- Tambahkan 14 tes installer; 85 tes lolos pada CPython 3.14.7 Linux. Instalasi bersih, uji impor, dan pemakaian ulang lingkungan berhasil.
- Seluruh paket biner Windows CPython 3.14 berhasil diresolusikan; Windows asli belum dieksekusi.
- Versi mesin/prompt dan identitas cache tetap 2.0.0 untuk kompatibilitas progres.

## 2.0.0 — 12 September 2026

- Ganti rekonstruksi HTML/EbookLib dengan penggantian text node pada XML asli dan penyalinan ZIP.
- Pertahankan inline markup, aset, navigasi, dan metadata; periksa struktur serta cakupan sebelum output.
- Tambahkan validasi XML/ZIP, parser entity lokal, source protection, atomic output, dan EPUBCheck opsional.
- Ganti snapshot cache JSON dengan transaksi SQLite, checksum, namespace per settings, dan kunci OS.
- Gunakan Gemini REST dengan schema JSON ketat, pemeriksaan finishReason, backoff, Retry-After, pemecahan batch, serta limit sesi/RPM/TPM/RPD persisten.
- Tambahkan worker antarmuka dengan jeda, pemulihan sesi, dan unduhan yang sesuai pilihan aktif.
- Tambahkan CLI inspect/offline/overwrite/glossary/instruction/epubcheck serta bootstrap yang dapat memulihkan instalasi gagal.
- Tambahkan regression tests, fixture EPUB 2/3, laporan audit, dan workflow CI.
- Perubahan kompatibilitas: khusus EPUB; cache JSON versi 1.x tidak diimpor; PDF/TXT tidak disertakan.
