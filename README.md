# Translator EPUB Inggris → Indonesia

Versi 2.2.0 — packing digerakkan budget output dan rotasi API key otomatis. Lihat PATCH_NOTES_2.2.0.md.
Versi 2.1.2 — memperbaiki ekspor Windows yang gagal dengan `[Errno 9] Bad file descriptor` setelah cache lengkap. Lihat PATCH_NOTES_2.1.2.md untuk hasil pemulihan dan batas pemeriksaannya. Perbaikan target manual, limiter bersama dan Local RPD dari 2.1.1 tetap ada.

Aplikasi Python lokal untuk menerjemahkan EPUB memakai Gemini API. Mesin mengganti teks di dalam struktur XHTML asli; tag dan isi arsip tidak dibuat ulang oleh AI. Aplikasi menyediakan antarmuka browser lokal dan CLI.

## Mulai di Windows

1. Gunakan CPython 3.11–3.14 64-bit biasa (dengan GIL), dari [python.org](https://www.python.org/downloads/windows/).
2. Ekstrak ZIP ke folder yang bisa ditulis, misalnya C:\GeminiEpubV2. Jangan menjalankannya langsung dari dalam ZIP.
3. Klik dua kali **MULAI_WINDOWS.bat**. Instalasi dependensi hanya dilakukan pada awal penggunaan atau ketika versi dependensi berubah.
4. Masukkan API key di kolom sandi, pilih EPUB, lalu klik **Mulai / lanjutkan**.
5. Jika kuota habis, tutup/jeda dan lanjutkan dengan EPUB serta pengaturan model/gaya/glosarium yang sama.
6. Setelah selesai, klik **Unduh EPUB Indonesia**. Hasil dan laporan integritas juga ada di folder **workspace/outputs**.

Untuk Linux/macOS:

~~~sh
sh mulai_linux_mac.sh
~~~

Antarmuka hanya mendengarkan 127.0.0.1. Ini aplikasi desktop lokal untuk satu pengguna; bukan layanan publik dengan login/multi-tenant.

### Memperbaiki instalasi yang gagal pada pandas / vswhere.exe

Versi 2.0.0 mengunci pandas 2.2.3 yang tidak menyediakan wheel untuk Python 3.14. Karena launcher memilih Python terbaru, pip kemudian mencoba kompilasi dari source dan meminta toolchain Visual Studio.

Versi 2.0.1 memakai pandas 2.3.3, yang [mendukung Python 3.14 dan menyediakan wheel](https://pandas.pydata.org/docs/whatsnew/v2.3.3.html). Installer memakai paket biner siap pakai, memeriksa versi Python, menjalankan pip check serta uji impor, lalu baru menandai instalasi sebagai selesai.

1. Tutup jendela aplikasi/installer.
2. Ekstrak ZIP 2.1.2 dan salin file program dalam folder gemini_novel_translator ke folder program lama, lalu pilih Replace untuk file program. Pastikan requirements.txt dan constraints.txt sama-sama diperbarui.
3. Jalankan MULAI_WINDOWS.bat kembali. Launcher akan melanjutkan instalasi pada .venv yang sudah ada; folder workspace dan cache v2 tetap dapat digunakan.

Perbaikan installer 2.0.1 tetap disertakan. Versi 2.1.0 mempertahankan ID potongan dan fingerprint cache 2.0.x; prompt request berikutnya dioptimalkan tanpa menghapus terjemahan yang sudah tersimpan.

## Yang dipertahankan

| Komponen EPUB | Perilaku versi 2 |
| --- | --- |
| Heading, bold, italic, span, drop cap | Tag dan atribut tetap. AI mengembalikan teks per posisi dengan konteks paragraf. |
| CSS, gambar, font, audio, video, file tambahan | Isi byte setiap aset tetap sama. Teks pada gambar/SVG dan audio tidak diterjemahkan. |
| Tautan, ID, footnote, struktur TOC | Tujuan href/src dan ID tetap. Label nav XHTML dan NCX diterjemahkan pada posisi asal. |
| Metadata | Title, author, identifier, cover, rights, refinement, dan timestamp asli tetap. Nilai bahasa Inggris diganti menjadi id. |
| Manifest, spine, urutan bab, properti EPUB | Dipertahankan. Tidak direkonstruksi oleh EbookLib. |
| Teks di div, li bertingkat, tabel, caption, tail node | Ikut diterjemahkan; tidak hanya paragraf p. |
| Teks alternatif | alt, title, dan aria-label dalam konten diterjemahkan. |
| Bahasa asing eksplisit, translate=no, class notranslate | Dipertahankan. |
| Script, CSS inline, code/pre, MathML, SVG | Struktur dan teks tetap. |

XML terjemahan ditulis sebagai UTF-8; tanda kutip XML, representasi entity, dan deklarasi encoding bisa berubah tanpa mengubah struktur. ZIP dikemas ulang, sehingga seluruh file EPUB tidak identik secara byte. Setiap aset di luar dokumen teks/OPF harus tetap identik secara byte.

Panjang terjemahan dapat mengubah pagination dan pemenggalan baris. Pada buku dengan posisi teks absolut/fixed layout, periksa overflow secara visual. Aplikasi tidak mengubah ukuran font atau CSS untuk memaksa teks muat.

## Gemini dan Free Tier

Default model: **gemini-3.5-flash-lite**. Per dokumentasi Google yang diperiksa 12 September 2026, model ini tersedia dengan harga input/output gratis pada Free Tier. Ketersediaan model, kuota, dan tier tetap mengikuti proyek pengguna:

- [Model Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Harga resmi Gemini](https://ai.google.dev/gemini-api/docs/pricing)
- [Cara kerja rate limit](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Lihat kuota proyek di AI Studio](https://aistudio.google.com/usage?tab=rate-limit)

Aplikasi tidak mengaktifkan billing dan tidak berpindah model otomatis. Gunakan proyek Free Tier jika ingin penggunaan gratis. Tidak ada parameter generateContent yang dapat memaksa proyek berbayar menjadi gratis.

Preset dibuat untuk batas proyek yang diberikan pengguna: **15 RPM, 250.000 input TPM, 500 RPD**. Angka ini bukan kuota universal seluruh akun/model. RPM/TPM tetap dibatasi 15/250.000. Official RPD Limit adalah referensi yang dapat diedit; Local RPD Safety Limit juga dapat diedit tanpa batas tetap 500. Mengubahnya tidak mengubah kuota Google. Model lain tetap dapat dipilih; sesuaikan batasnya dengan AI Studio.

| MODE | RPM hard | TPM target | TPM hard | RPD lokal | Target input/request | Maks. input/request | Workers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Safe | 10 | 210.000 | 230.000 | 470 | 14.000 | 18.000 | 1 |
| Balanced | 13 | 225.000 | 240.000 | 490 | 24.000 | 30.000 | 2 |
| Aggressive (default) | 13 | 240.000 | 248.000 | 490 | 30.000 | 36.000 | 2 |

Sejak 2.2.0, target input di atas adalah rel pengaman; ukuran batch aktual
digerakkan budget output: packer mengisi sampai prediksi output menyentuh
~90% batas output memakai rasio ekspansi adaptif (terukur ~1,44 untuk
EN→ID). Pada praktiknya satu request membawa ~2x teks dibanding 2.1.2
untuk kuota RPD yang sama.

Auto Tune default **OFF**. Target/maksimum input dapat diubah manual, hingga batas aplikasi 40K. Advanced Settings menyediakan override manual serta RPD Safe 470 / Aggressive 490 / Maximum 498. Batas sesi default 490 mencakup semua percobaan. Default timeout 600 detik memberi ruang bagi output panjang; dapat diubah. Tidak ada sleep tetap antarpengiriman ketika kuota tersedia.

### Token packing dan adaptive control

- Request dipacking berurutan lintas chapter/section/paragraf; satu chapter kecil tidak wajib menjadi satu request. Batas dihitung pada **seluruh request**, termasuk instruksi sistem, gaya, instruksi khusus, glossary relevan, referensi konteks, ID, label dokumen, JSON dan skema.
- Master glossary disimpan di pengaturan/checkpoint lokal. Hanya istilah yang cocok pada batas kata (case insensitive, termasuk nama yang melintasi inline run) di teks/konteks batch yang dikirim. Konteks paragraf lengkap tidak diulang; konteks tambahan yang sama memakai satu referensi.
- Parser dan penulis EPUB tetap bekerja pada XML lokal. Gemini menerima teks per ID dan petunjuk format, lalu mengembalikan JSON per ID; AI tidak menulis ulang HTML. Tag/CSS/gambar/link/footnote tidak dibelah oleh batching.
- Boundary paragraf diprioritaskan. Potongan internal v2 yang lama tetap dipakai agar cache cocok; paragraf sangat panjang dapat melintasi request bila tidak muat. Teks sumber, entity dan tag tidak dipotong ulang oleh packer.
- Untuk Gemini 3.5 Flash Lite, REST memakai thinkingLevel MINIMAL, temperature 0.2, dan maxOutputTokens 65536. Perkiraan output memasukkan ekspansi Indonesia, JSON dan ruang thinking, dan dibatasi pada 90% kapasitas output. Batas output, jumlah teks tersisa dan batas TPM hard dapat membuat batch aktual lebih kecil daripada target manual; nilai target global tetap.
- Respons kosong, ID hilang/duplikat/tambahan, JSON rusak, HTML baru pada slot teks, finishReason hilang atau MAX_TOKENS tidak disimpan. Batch dipecah dua pada batas unit/run dan tiap bagian yang berhasil langsung di-checkpoint. Saat Auto Tune OFF, pemecahan hanya berlaku untuk batch gagal; target global tidak turun dan nilai kecil dari tuning lama diabaikan. Jika Auto Tune sengaja diaktifkan, adaptasi target tetap berlaku. Batch rusak yang besar tidak dikirim ulang identik.
- Bila diaktifkan, Auto Tune mulai pada target TPM 220K (atau target profil jika lebih rendah). Tiap 10 sukses menaikkan target TPM sekitar 4% menuju target profil. Target batch boleh naik 2K setelah 10 sukses dan ada bukti batch mendekati target, hingga maksimum yang dipilih.
- 429 TPM menurunkan target TPM 10%; 429 RPM menurunkan RPM efektif dan concurrency. Pemulihan memerlukan 20 sukses dan naik sekitar 4%, bukan langsung kembali ke maksimum. State tuning disimpan per model/profil. Mengubah anggaran manual membuat kebijakan tuning baru, tanpa mengubah cache terjemahan.
- Semua worker berbagi reservasi SQLite yang atomik. Jendela 60 detik memakai expiry request yang tepat; koreksi token aktual dapat membangunkan worker lebih awal. Target TPM boleh memakai sedikit ruang untuk estimasi request aktif, tetap di bawah TPM hard. Tidak dibuat batch kecil hanya untuk mengisi sisa TPM.

### Kontrol Local RPD

Di Advanced Settings, edit **Official RPD Limit** dan **Local RPD Safety Limit**. Di **Local RPD accounting**, pilih key bila memakai lebih dari satu, edit **Current Local RPD Used**, lalu klik **Simpan RPD lokal**. Tombol **Reset Local RPD to 0** hanya mengubah penghitung lokal key terpilih; tidak menghapus riwayat RPM/TPM, cooldown API, cache, checkpoint, glossary atau EPUB. Kontrol ini tersedia setelah request aktif selesai.

### Rotasi API key otomatis (2.2.0)

Kuota Google berlaku per project, bukan per key. Isi beberapa key — di UI lewat
kolom utama + expander "API key tambahan", di CLI lewat `--api-key` yang dapat
diulang atau env `GEMINI_API_KEYS` (koma/baris baru) — dan tool otomatis
beralih ke key berikutnya saat satu key mencapai batas hariannya, tanpa jeda
manual dan tanpa memakan budget retry. Akuntansi RPD/RPM/TPM dan cooldown
dicatat per key (scope digest; key asli tidak pernah masuk database).
Sesi hanya dijeda bila semua key habis. Mode satu key tidak berubah.

"Changing or resetting Local RPD only changes the translator's internal counter. It does not reset Google's real API quota."

Koreksi hitungan disimpan pada state tambahan yang sudah tersedia dalam database, tanpa mengubah format cache. Hanya percobaan HTTP generation (termasuk retry) menambah Local RPD; countTokens dan pekerjaan lokal tidak. Koreksi berlaku untuk hari Pasifik berjalan, lalu reset harian otomatis tetap berlaku. Batas RPD yang dipilih bertahan setelah restart.

### countTokens dan akurasi token

Default **auto** melakukan satu kalibrasi countTokens awal per model/workspace bila belum ada data, lalu memperbarui estimasi dari promptTokenCount di usageMetadata pada setiap respons. Panggilan countTokens menerima generateContentRequest lengkap, termasuk systemInstruction dan generationConfig. Hasil hitung disimpan berdasarkan hash request. Mode **always** menghitung setiap batch; mode **estimate** tidak memakai endpoint hitung.

Estimator lokal bukan tokenizer Gemini yang identik. Sebelum kalibrasi dipakai margin 12%, sesudahnya 5% plus overhead tetap; koreksi naik langsung, koreksi turun bertahap. Batas maksimum input yang dipilih (default 18K, dapat dinaikkan hingga 40K) adalah batas preflight menurut countTokens jika tersedia atau estimasi tersebut. Angka aktual dapat berbeda pada mode estimasi; penggunaan rolling diperbarui ketika API mengembalikan token aktual. Gunakan always bila perlu preflight aktual setiap batch, dengan konsekuensi panggilan tambahan.

Dokumentasi countTokens tidak menetapkan jaminan bahwa pemanggilannya bebas dari seluruh batas proyek ini. Karena itu **countTokens tetap dicatat konservatif pada RPM/TPM dan batas sesi, tetapi tidak menambah Local RPD**; dashboard memisahkan jumlahnya. Auto menghindari menggandakan panggilan per batch. Jika endpoint hitung gagal/tidak didukung, mode tersebut berhenti memanggilnya pada sesi berjalan dan melanjutkan estimasi; 429 harian tetap menghentikan sesi. Angka lokal dapat lebih tinggi dari dashboard Google karena kebijakan konservatif ini.

Target utilisasi 92–98% merupakan tujuan, bukan jaminan. Ukuran output, fragmentasi format, sisa buku, latensi Gemini dan jumlah worker dapat membatasi throughput. Prioritasnya hasil utuh dan teks per RPD, bukan memaksakan 15 RPM atau menambah teks/instruksi pengisi agar TPM terlihat penuh.

### Dashboard

Menampilkan RPM/TPM rolling gabungan, batas hard dan target adaptif, RPD tersimpan/tersisa/reset, rata-rata input/output per generation yang valid, target batch, workers aktif, sukses, 429, retry, panggilan hitung, karakter/kata/token sumber yang selesai, throughput sesi, sisa request, kebutuhan RPD dan ETA.

Token input/output memakai usageMetadata jika ada; request aktif dan respons tanpa usage memakai estimasi untuk input. Token **sumber saja** merupakan estimasi lokal, karena usage API mencakup seluruh prompt. Karakter/kata adalah teks sumber yang selesai diterjemahkan, bukan panjang hasil Indonesia. Throughput mengecualikan progres yang sudah tersimpan sebelum sesi; ETA berasal dari throughput aktual sesi dan belum memasukkan jeda reset harian. Peringatan RPD muncul bila perkiraan sisa pekerjaan melebihi jatah hari ini.

Kuota API berlaku per proyek, bukan per key. Hitungan aplikasi tersimpan per model dalam satu folder progres dan dibagi oleh semua key yang memakai folder tersebut. Penggunaan melalui aplikasi/perangkat/folder lain tidak terlihat; respons 429 tetap menjadi acuan.

429 sementara/408/5xx/gangguan koneksi memakai backoff 5/10/20/40/... detik dengan jitter, maksimal 120 detik bila server tidak memberikan waktu tunggu. Retry-After dan google.rpc.RetryInfo dihormati. Kuota harian atau kuota nol menjeda proses. RPD reset tengah malam America/Los_Angeles, termasuk penyesuaian DST. Setiap percobaan request, termasuk retry dan pemecahan batch, dihitung.

Teks buku dikirim ke Google. Ketentuan pemrosesan data Free Tier mengikuti [halaman harga dan ketentuan Google](https://ai.google.dev/gemini-api/docs/pricing). API key tidak ditulis ke cache, laporan, atau URL.

### Log diagnostik kegagalan (2.2.1)

Setiap batch yang gagal dicatat ke `workspace/diagnostics/failures.jsonl` (satu baris JSON per kegagalan): `invalid_response`/`repack` dari validasi pipeline beserta pesan error persisnya, serta `http_retry`/`timeout`/`transport_error` dari level HTTP. Berguna untuk menganalisis penyebab retry tinggi. Log hanya berisi pesan error, ukuran batch, dan ID potongan teks — tanpa isi respons/request atau API key.

### Pengulangan batch untuk kesalahan mekanis (2.2.2, diubah 2.2.3)

Batch yang gagal validasi karena kesalahan mekanis yang stokastik (ID hilang/duplikat, jumlah unit/potongan/skema tidak cocok, teks kosong, markup HTML) kini **dicoba ulang sekali dengan urutan unit dibalik** sebelum dibelah (sejak 2.2.3; pengulangan identik di 2.2.2 terbukti tidak efektif karena model nyaris deterministik di temperature 0,2). Kesalahan deterministik (terpotong MAX_TOKENS, respons terlalu pendek, RepackRequired) tetap langsung dibelah. Tujuannya memutus cascade split yang membakar banyak request untuk konten yang sama. Validasi tetap ketat dan split tetap menjadi fallback, sehingga jaminan kelengkapan teks tidak berubah. Angka `Retry` di dashboard mencakup pengulangan ini.

### Salvage parsial (2.2.4)

Sejak 2.2.4 respons yang tidak sempurna tidak lagi dibuang utuh: setiap potongan dinilai sendiri, potongan yang valid langsung disimpan, dan hanya potongan yang rusak/hilang yang diminta ulang dalam satu batch susulan kecil. Jalur balik-sekali → belah di atas kini hanya berlaku bila satu batch tidak menyisakan satu potongan valid pun. Kejadian salvage tercatat sebagai `"kind": "salvage"` di failures.jsonl (dengan `salvaged_runs` vs `batch_runs`); angka `Retry` di dashboard mencakup batch susulan ini. Prompt, skema, dan ID tidak berubah, jadi cache semua versi 2.0.x–2.2.3 tetap kompatibel.

### Domain fiksi / non-fiksi dan generate ulang (2.3.0)

Di expander "Gaya dan istilah" (CLI: `--domain`) kini ada pilihan domain. **Fiksi** memakai instruksi novel seperti biasa. **Non-fiksi** memakai instruksi khusus yang mengutamakan akurasi fakta, angka, tanggal, satuan, dan istilah teknis dengan register netral, tanpa hiasan sastra — cocok untuk esai, sejarah, sains, dan biografi. Hasil kedua domain tersimpan di cache yang terpisah, dan cache fiksi lama tetap kompatibel penuh (identitas cache hanya berubah untuk domain non-fiksi).

Checkbox **"Generate ulang dari nol"** (CLI: `--fresh`) mengabaikan cache buku yang sedang dibuka: seluruh potongan diterjemahkan ulang dan hasil lama ditimpa. Berguna untuk membandingkan kualitas antar domain, gaya, atau model pada buku yang sama. Request tetap dihitung ke kuota seperti biasa.

### Penyedia OpenAI-compatible (2.4.0)

Di sidebar kini ada pilihan **Penyedia AI**. Selain Gemini, tool dapat memakai gateway yang kompatibel dengan API OpenAI (chat completions), misalnya bansosai: isi **Base URL gateway** (berakhiran `/v1`), **API key gateway**, dan **ID model** sesuai katalog gateway (mis. `claude-sonnet-4-5`). CLI: `--provider openai --oa-base-url ... --model ...` dengan key dari `--api-key` atau env `OA_API_KEY`. Key gateway terpisah dari key Gemini dan hasilnya tersimpan di cache tersendiri, jadi kedua penyedia tidak saling menimpa. Prompt, validasi, salvage, dan batas RPM/TPM/RPD lokal berlaku sama. Perlu diingat: teks buku dikirim ke server gateway pihak ketiga, dan kuota promosi gateway dapat berubah/berakhir kapan saja. Untuk model berbatas output kecil seperti deepseek-3.2, turunkan **Maksimum output token** ke 8000.

## Cache, jeda, dan resume

Folder **workspace/cache** menyimpan database **translations.sqlite3** dan kunci proses **workspace.lock**.

- Setiap batch yang berhasil divalidasi disimpan dalam satu transaksi SQLite dengan synchronous=FULL.
- Klik **Jeda setelah request aktif**: request baru dihentikan. Semua respons valid dari request yang sudah aktif tetap disimpan, termasuk ketika worker lain mencapai RPD/error. Request aktif dapat bertahan sampai timeout.
- Refresh browser dapat menghubungkan kembali dashboard ke job dalam proses aplikasi yang sama. Menutup tab browser tidak selalu menghentikan worker. Gunakan tombol jeda; menutup proses aplikasi tetap memungkinkan resume dari batch yang telah di-commit.
- Jalankan kembali dengan file yang isinya sama. Nama file boleh berubah.
- Model/gaya/glosarium/instruksi yang berbeda memakai ruang cache terpisah. Kembali ke pengaturan lama tetap dapat memakai progres lama.
- RPM/TPM/RPD, batas sesi, timeout, concurrency, countTokens, output budget, temperature dan target token boleh diubah tanpa kehilangan cache. Hasil yang sudah valid tidak diterjemahkan ulang otomatis.
- Jangan menghapus database bila ingin melanjutkan. Jika memindahkannya, jeda/tutup seluruh proses lalu salin seluruh folder cache. Gunakan penyimpanan lokal, bukan folder sinkronisasi aktif atau network share.
- Hanya satu proses boleh memakai folder progres yang sama. Kunci dilepas otomatis oleh sistem operasi jika proses mati.
- Jika cache tidak dapat ditulis, proses berhenti sebelum mengirim batch berikutnya. Tidak ada klaim progres tersimpan sebelum transaksi selesai.

Untuk menempatkan seluruh workspace di lokasi lain, atur environment variable **EPUB_TRANSLATOR_WORKSPACE** sebelum menjalankan aplikasi. CLI juga menyediakan **--cache-dir**.

### Update ke 2.1.2 dengan progres yang sedang berjalan

1. Klik Jeda, tunggu worker aktif selesai, lalu tutup jendela aplikasi/terminal lama.
2. Salin file program dari ZIP 2.1.2 ke folder program lama. **Pertahankan workspace terbaru di PC.** Paket ini menyertakan workspace persis saat diunggah; jika progres di PC sudah lebih baru, jangan menimpanya dengan snapshot dalam ZIP.
3. Jalankan MULAI_WINDOWS.bat. Perbaikan pandas/installer 2.0.1 tetap ada; tidak ada dependensi tambahan.
4. Di **Lanjutkan checkpoint lama**, pilih progres yang sesuai dan klik **Muat pengaturan checkpoint**. Unggah EPUB sumber yang sama. Hash delapan karakter dan jumlah potongan membantu mengenali checkpoint.
5. Pilih Aggressive dan klik Mulai / lanjutkan. Cache lengkap dapat diekspor tanpa API key. Counter RPD yang sudah ada tetap digunakan.

Identitas ENGINE_VERSION=2.0.0 dan PROMPT_VERSION=2 sengaja tetap untuk kontrak cache, sedangkan versi aplikasi 2.1.2. Payload fingerprint unit/run tidak berubah. Metadata block_key baru hanya membantu memilih batas paragraf dan tidak masuk hash. Tabel lama tetap utuh; tabel metrik/tuning ditambahkan. Fixture checkpoint yang dibuat dengan paket 2.0.1 asli diuji dapat dilanjutkan tanpa mengirim ulang atau menimpa 14 potongan tersimpan.

Bila tidak ada checkpoint yang terlihat, pastikan aplikasi memakai workspace lama. Jangan mulai menerjemahkan seluruh buku ulang sebelum lokasi folder benar. Salinan checkpoint dalam unggahan terbaru sudah diuji dapat dimuat; perubahan setelah unggahan pada PC tidak tersedia untuk diperiksa.

### Riwayat upgrade dari 1.2

Ekstrak versi 2 ke folder baru. Jangan hapus program/progres versi lama sebelum memastikan hasil baru sesuai.

Cache JSON 1.x menyimpan paragraf yang sudah diratakan dan tidak punya pemetaan text node untuk bold/italic/gambar. Versi 2 **tidak mengimpor cache 1.x secara otomatis** karena tidak dapat memulihkan pemetaan format itu dengan aman. File cache lama tidak diubah atau dihapus. Cache/resume penuh pada versi 2 memakai SQLite dan berjalan antar-sesi versi 2.

Versi ini sengaja khusus EPUB. Konversi PDF dan ekspor TXT dari versi 1.x tidak disertakan.

## CLI

Setelah launcher menyiapkan .venv, gunakan Python dari .venv. Contoh Windows:

~~~powershell
.\.venv\Scripts\python.exe cli.py "D:\Novel\book.epub"
~~~

CLI meminta key dengan input tersembunyi jika GEMINI_API_KEY belum diatur. Jangan menaruh key dalam argumen command line. Pada terminal noninteraktif, isi environment variable tersebut. Untuk rotasi multi-key: `--api-key` dapat diulang, atau isi `GEMINI_API_KEYS` (koma/baris baru). Key tidak pernah ditulis ke cache atau laporan.

~~~sh
python cli.py book.epub --mode Aggressive --workers 2 --input-tokens 36000 --max-input-tokens 40000
python cli.py book.epub --api-key KEY_PROJECT_A --api-key KEY_PROJECT_B
python cli.py book.epub --target-tpm 240000 --tpm 248000 --rpm 15 --rpd 490
python cli.py book.epub --count-tokens always --no-auto-tune
python cli.py book.epub --inspect
python cli.py book.epub --output exported_from_cache.epub --offline
python cli.py book.epub --glossary glossary.txt --instruction instruction.txt
python cli.py book.epub --output book_Indonesia.epub --overwrite
~~~

Argumen lama --chunk-chars tetap diterima untuk kompatibilitas pemanggil, tetapi tidak lagi menentukan batching; gunakan --input-tokens. Ctrl+C meminta jeda. Exit code: 0 selesai/inspect berhasil; 3 dijeda; 1 error. Error penggunaan argumen memakai exit code argparse 2.

**--offline** hanya mengekspor bila cache sudah lengkap. Tidak melakukan request dan tidak membutuhkan key.

Glosarium UTF-8, satu pasangan per baris:

~~~text
Klein Moretti = Klein Moretti
Beyonder = Beyonder
Sequence = Sequence
Church of the Evernight Goddess = Gereja Dewi Malam Abadi
~~~

## Pemeriksaan sebelum output

Output ditulis ke file sementara di folder tujuan. Sebelum dipublikasikan, aplikasi memeriksa CRC ZIP, daftar anggota, mimetype pertama tanpa kompresi, XML yang dapat diparse, kesamaan struktur tag, ID/href/src/CSS, byte aset, metadata yang dipertahankan, dan cakupan semua potongan terjemahan.

Respons AI dengan ID hilang/duplikat/tambahan, JSON rusak, teks kosong, karakter XML ilegal, pemotongan MAX_TOKENS, atau blokir konten tidak dianggap sukses. Respons tidak lengkap bisa dicoba dalam kelompok yang lebih kecil; hasil yang lolos tetap langsung di-commit.

Sumber tidak boleh ditimpa, termasuk melalui hard link. Output yang sudah ada tidak ditimpa kecuali pengguna memilihnya. Jika penulisan gagal karena file dibuka aplikasi lain, output lama tetap ada dan cache dapat dipakai kembali.

Untuk pemeriksaan standar EPUB yang lebih lengkap, unduh [EPUBCheck](https://www.w3.org/publishing/epubcheck/), instal Java, lalu:

~~~sh
python cli.py book.epub --epubcheck-jar /path/to/epubcheck.jar
~~~

Jika EPUBCheck menghasilkan error, file final tidak dipublikasikan. EPUBCheck bersifat opsional dan tidak diunduh otomatis oleh aplikasi.

## Batas yang diketahui

- Dukungan utama: EPUB 2/3 dengan satu rendition dan spine XHTML. Tidak ada penghapusan DRM.
- Font obfuscation IDPF/Adobe dipertahankan bersama identifier. Enkripsi lain dan tanda tangan digital ditolak.
- EPUB dengan XML rusak, entity khusus, path ZIP tidak aman, CRC rusak, atau referensi manifest/spine hilang ditolak. Entity XHTML standar seperti nbsp diproses lokal tanpa mengunduh DTD.
- Referensi link/anchor yang sudah rusak di sumber dicatat; tidak diubah diam-diam.
- Batas default: arsip 150 MiB, total dekompresi 300 MiB, 32 MiB per anggota, 10.000 anggota.
- Tag penekanan tetap di posisinya. Kualitas pembagian terjemahan pada kata yang terbelah/span/drop cap dan pergeseran makna penekanan masih perlu diperiksa pada sampel hasil AI.
- Pemeriksaan integritas bukan pemeriksaan linguistik. AI masih bisa menghasilkan padanan yang keliru, nama tidak konsisten, atau teks yang belum diterjemahkan. Potongan panjang yang identik dengan sumber dicatat dalam laporan.
- Tidak ada uji API Gemini live dalam audit ini. Pengujian API menggunakan transport simulasi; akses key, kuota akun, dan kualitas terjemahan novel nyata harus diperiksa saat penggunaan.
- Launcher Windows dan penguncian file diuji secara simulasi/Linux, bukan pada mesin Windows fisik.

## Pengujian pengembang

~~~sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python tools/validate_release.py --epubcheck-jar /path/to/epubcheck.jar
python tools/benchmark_packing.py
~~~

Seluruh tes default berjalan offline, tanpa key. Script validasi membuat EPUB sintetis sendiri dan memakai penerjemah deterministik, bukan Gemini. Lihat **AUDIT_PERFORMANCE.md**, **AUDIT.md**, dan folder **validation** untuk bukti pengujian paket ini. Workflow GitHub Actions untuk Linux/Windows disertakan; workflow tersebut belum dijalankan dalam sesi audit.
