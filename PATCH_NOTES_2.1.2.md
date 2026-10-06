# Perbaikan ekspor dan hasil pemeriksaan progres

Error `[Errno 9] Bad file descriptor` berasal dari `EpubBook.export()` di
`novel_translator/epub.py`: EPUB sementara dibuka dengan `rb` sebelum `os.fsync()`.
Patch menggantinya menjadi `r+b`. Windows memerlukan akses tulis untuk
[FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers).
Kesalahan disk tetap diteruskan; validasi XML, CRC dan aset tetap berjalan sebelum
output diterbitkan.

## Apakah progres hilang?

Tidak. Snapshot unggahan berisi **40.899/40.899 blok cache valid** untuk
**Deep Sea Embers [HTL c1-803]**. Seluruh fingerprint cocok, checksum benar,
dan pemeriksaan integritas SQLite menghasilkan `ok`. Tidak ada blok yang hilang.
Status `failed` terjadi setelah penyimpanan terjemahan, pada tahap ekspor.

EPUB berhasil dibentuk dari salinan cache dengan **0 request Gemini**. Semua
baris terjemahan dan riwayat request dalam salinan tetap sama. File di seluruh
`workspace` asli dibandingkan dengan SHA-256 saat ekstraksi dan tetap identik.
Checkpoint lama 5.004 blok untuk pengaturan berbeda juga tetap dapat dimuat.

## Batas kelengkapan bahasa dan format

- Counter 100% berarti setiap posisi teks memiliki hasil tersimpan. Ini belum
  berarti seluruh hasil berbahasa Indonesia: **187 potongan masih identik dengan
  sumber**, termasuk nama/seruan, **53 posisi judul bab**, serta beberapa kalimat
  Inggris. Tiga potongan panjang berada di bab 41 (dua potongan) dan bab 63.
  Daftar lengkap ada di `validation/v2.1.2/unchanged-runs.json`.
- Cache dipertahankan apa adanya; potongan tersebut tidak ditimpa atau dikirim
  ulang otomatis. Pemeriksaan ini bukan penyuntingan bahasa seluruh novel.
- Pemeriksaan internal hasil: **816 anggota ZIP**, CRC valid, struktur XML serta
  atribut/tautan dipertahankan, dan byte aset tidak berubah.
- **EPUBCheck 5.3.0 belum lolos:** sumber mempunyai 876 kejadian error RSC-005;
  hasil 867. Keduanya memiliki 803 pasangan berkas/pesan yang sama, tanpa pasangan
  baru pada hasil. Masalah sumber berupa teks atau elemen `em` di lokasi yang
  tidak diizinkan model konten XHTML EPUB 2. Struktur sumber tidak dinormalisasi
  oleh patch ini. Laporan ini tidak menyatakan EPUB memenuhi seluruh standar.

## Memasang perbaikan

1. Tutup aplikasi dan terminal Streamlit lama.
2. Salin file program dari paket 2.1.2 ke folder program yang dipakai.
   **Pertahankan `workspace` dan lingkungan Python terbaru di PC.** Snapshot di
   paket adalah saat unggahan, bukan progres yang mungkin bertambah sesudahnya.
   Perbaikan fungsional hanya berada di `novel_translator/epub.py`; penanda versi
   ada di `app.py`, `novel_translator/__init__.py`, dan `pyproject.toml`.
3. Jalankan `MULAI_WINDOWS.bat`. Muat checkpoint **40.899/40.899**, unggah EPUB
   sumber yang sama dan pertahankan pengaturan checkpoint, lalu klik
   **Mulai / lanjutkan**. Cache lengkap dapat diekspor tanpa API key.
4. EPUB hasil pemulihan juga disediakan sebagai unduhan terpisah.

## Pemeriksaan yang dijalankan

**16 tes lulus, 0 gagal** pada CPython 3.14.7 Linux:

- Sembilan tes `test_manual_controls.py`: impor/default, startup Streamlit HTTP,
  cache aktif pada salinan, target manual, split lokal, dua worker berbagi quota,
  edit/reset RPD persisten, dan pause/resume.
- Tiga tes baru `test_export_recovery.py`: ekspor EPUB 2/3 dengan simulasi akses
  flush Windows, serta kegagalan sinkronisasi disk diikuti resume cache tanpa API.
- Empat tes terkait: struktur/aset/metadata/TOC EPUB 2/3, perlindungan output saat
  validasi gagal, dan ekspor cache tanpa key setelah pengaturan operasional berubah.
- Syntax check 17 file dan impor seluruh modul translator/dependensi lolos.

Sebelum perbaikan, dua kasus simulasi Windows gagal tepat dengan EBADF pada
`os.fsync()`; keduanya lulus setelah patch. Windows asli dan API Gemini langsung
tidak dijalankan. Bukti hasil tersedia dalam `validation/v2.1.2`.

Format cache, identitas mesin/prompt dan pengaturan terjemahan tidak diubah.
