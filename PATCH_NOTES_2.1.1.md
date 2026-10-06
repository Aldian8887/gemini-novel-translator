# Patch 2.1.1

Melanjutkan proyek yang diunggah beserta progresnya. Perubahan runtime dibatasi pada:

| File | Perbaikan |
| --- | --- |
| `novel_translator/adaptive.py` | Auto Tune OFF mengabaikan penurunan target lama dan mempertahankan target manual saat retry/split. |
| `novel_translator/token_budget.py` | Target TPM adaptif tidak lagi mengecilkan ukuran batch normal; quota mengatur penjadwalan. |
| `novel_translator/rate_limit.py` | Check + reservasi memakai lock bersama dan transaksi BEGIN IMMEDIATE; timestamp diambil setelah lock. Cooldown kedaluwarsa dibersihkan, histori RPD tetap tersedia. |
| `novel_translator/preferences.py` | Edit/reset RPD persisten melalui state tambahan yang sudah tersedia, tanpa perubahan format cache. |
| `novel_translator/models.py` | Default 14K/18K, 13 RPM, dua worker, Auto Tune OFF. Official/local RPD berupa bilangan bulat positif yang dapat diubah. |
| `app.py` | Kontrol Official RPD Limit, Local RPD Safety Limit, Current Local RPD Used dan Reset Local RPD to 0. Callback mempertahankan widget glossary/instruksi/unggahan. |
| `cli.py` | Default mengikuti patch; `--auto-tune` untuk mengaktifkan dan `--official-rpd` untuk referensi RPD. `--no-auto-tune` tetap diterima. |

Versi aplikasi, README, ekspektasi tes yang terdampak, dan bukti validasi ikut diperbarui. Script validasi yang telah dimodifikasi pengguna dipertahankan.

Saat Auto Tune OFF, split 14K menjadi dua bagian hanya berlaku pada batch gagal. Batch normal berikutnya kembali memakai anggaran 14K. Ukuran aktual masih mengikuti batas output, maksimum input, TPM hard dan teks tersisa; target manual global tidak ditimpa oleh retry. Saat Auto Tune sengaja diaktifkan, adaptasi yang sudah ada tetap berlaku. RPM 429 tidak mengubah target batch.

Local RPD hanya bertambah untuk percobaan generation, termasuk retry; countTokens tetap masuk RPM/TPM dan batas sesi, tetapi tidak masuk RPD. Edit/reset menyimpan koreksi hitungan hari berjalan. Reset tidak menghapus histori request, cooldown API, translations, jobs atau file workspace. Official RPD yang diedit adalah referensi, bukan perubahan quota server.

> Changing or resetting Local RPD only changes the translator's internal counter. It does not reset Google's real API quota.

**Validasi akhir: 9 passed, 0 failed.** Pemeriksaan terfokus mencakup impor/default, startup Streamlit HTTP 200, pemuatan cache unggahan pada salinan sementara, target manual/persistensi, split lokal, limiter dua worker, edit/reset UI RPD dan persistensi, keutuhan progres saat reset, serta pause/resume dua worker. API disimulasikan; tidak memakai kuota Gemini nyata. JUnit tersedia di `validation/v2.1.1/focused-tests.xml`. Suite regresi penuh tidak dijalankan pada patch ini sesuai permintaan.

Unggahan mempunyai dua checkpoint (5.004 dan 25.532 blok), total **30.536 blok tersimpan**. Kedua checkpoint berhasil dimuat dengan fingerprint asli. Hash seluruh file workspace sebelum/sesudah patch identik. Pengujian tidak menulis ke workspace asli. Identitas ENGINE_VERSION/PROMPT_VERSION dan schema cache tetap.

Untuk memperbarui folder program lama, salin file program versi ini dan pertahankan workspace terbaru di PC. ZIP menyertakan snapshot workspace persis saat diunggah; jangan menimpa progres PC yang sudah lebih baru dengan snapshot tersebut. Jalankan `MULAI_WINDOWS.bat`, muat pengaturan checkpoint yang sesuai, lalu lanjutkan EPUB yang sama.
