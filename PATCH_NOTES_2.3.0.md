# Patch Notes v2.3.0 — Domain Fiksi/Non-Fiksi + Generate Ulang

## Fitur 1 — Domain terjemahan

- Pilihan **Fiksi / Non-fiksi** di UI (expander "Gaya dan istilah") dan CLI
  (`--domain fiction|nonfiction`).
- Non-fiksi memakai instruksi sistem khusus (`SYSTEM_NONFICTION`): akurasi
  fakta, angka, tanggal, satuan, nama orang/lembaga/teori, dan istilah
  teknis diutamakan; register netral-jernih; tanpa hiasan sastra. Kalimat
  protokol JSON/ID identik dengan fiksi, jadi validasi/salvage tidak
  berubah perilakunya.
- Jika gaya masih default fiksi saat domain non-fiksi dipilih, gaya efektif
  otomatis diganti ke default non-fiksi ("Akurat, jernih, dan lugas seperti
  buku nonfiksi Indonesia modern") agar prompt tidak bertabrakan; gaya
  eksplisit pengguna selalu dihormati.
- Preset gaya non-fiksi ditambahkan ke daftar gaya UI.

## Fitur 2 — Generate ulang dari nol

- Checkbox UI "Generate ulang dari nol (abaikan cache buku ini)" / CLI
  `--fresh`. Cache terjemahan lama diabaikan, seluruh potongan diterjemahkan
  ulang dan hasil baru menimpa cache + output lama (mode ini otomatis
  mengizinkan timpa output).
- Gunanya: membandingkan kualitas antar domain/gaya/model pada buku yang
  sama tanpa menghapus workspace secara manual.

## Kompatibilitas cache (penting)

- `settings()` hanya menulis kunci `domain` bila bukan `"fiction"`.
  Akibatnya job_id, identitas cache, dan instruksi fiksi pengguna lama
  **byte-identik** dengan 2.2.x — terbukti lewat perbandingan langsung
  terhadap kode 2.2.3 (settings + teks instruksi sama persis). Cache
  40.899 blok Deep Sea Embers dan semua progres berjalan tetap terbaca.
- Hasil non-fiksi tinggal di namespace cache sendiri (job_id berbeda),
  tidak pernah menimpa hasil fiksi, dan sebaliknya. Instruksi fiksi sama
  sekali tidak berubah; `ENGINE_VERSION=2.0.0`, `PROMPT_VERSION=2`.

## Bukti

- Offline: py_compile bersih; settings/instruksi fiksi identik dengan
  versi lama; smoke test salvage (parsial/racun/legacy) tetap SEMUA PASS;
  validasi domain menolak nilai di luar {fiction, nonfiction}.
- Live (2 key asli): 4 konfigurasi lintas-domain atas kutipan Austen Bab
  I–II dan Darwin Bab I selesai tanpa kegagalan validasi; mode generate
  ulang pada workspace yang sudah selesai terbukti mengeluarkan request
  generation sungguhan (bukan selesai instan dari cache).
- Analisis kualitas lengkap: `laporan_kualitas_v2.3.0.md` — ringkasnya,
  domain terasa nyata (54/63 paragraf berubah), kesalahan dominan justru
  di istilah jebakan yang bisa ditutup lewat glosarium.
