# Patch Notes 2.4.1 — hemat token jalur gateway (OpenAI-compatible)

Fokus rilis ini: menurunkan pemakaian token di situs aggregator (mis. bansosai)
tanpa mengubah hasil terjemahan. Jalur Gemini TIDAK berubah sama sekali:
ENGINE_VERSION=2.0.0 dan PROMPT_VERSION=2 tetap; settings/job_id mode Gemini
byte-identik dengan 2.4.0 (terverifikasi); file mesin inti tidak disentuh.

## Yang berubah (khusus provider "openai")

1. **Batas output per model gateway.** claude-sonnet-4-5 dan qwen-3-8-max kini
   memakai batas 32.768 token output per batch (sebelumnya semua model
   non-Gemini dibatasi 16.384). --max-output-tokens tetap menjadi batas atas.
   Batch yang lebih besar berarti biaya tetap tiap request — instruksi sistem
   (±253 token) dan overhead gateway (±121 token) — terbagi ke lebih banyak
   teks. Simulasi pada sampel 314 unit: ~3 batch menjadi ~2 batch.
2. **Alias ID pendek di lapisan transport.** ID unit/run asli (mis.
   "d5n15-text-0") diganti alias u0../r0.. selama perjalanan ke gateway, lalu
   dipetakan kembali ke ID asli sebelum validasi. Validator, cache, dan
   identitas unit tidak pernah melihat alias. Pada sampel: payload input
   −6%, rangka JSON output −12%, dan ID pendek lebih jarang salah disalin
   model (salah satu penyebab respons invalid).
3. **Watchdog anggaran token sesi.** Flag baru `--oa-token-budget N`
   (0 = mati). Setiap respons dicatat ke penghitung sesi — memakai angka
   usage asli bila gateway mengembalikannya, selain itu estimasi lokal.
   Sebelum batch yang akan melewati anggaran dikirim, job berhenti bersih
   (progres tersimpan) dan bisa dilanjutkan kapan pun. Baris status kini
   menampilkan "terpakai sesi ini ~X token" selama anggaran aktif.

Juga dimasukkan resmi: hotfix parse JSON toleran tanda kutip mentah
(`parse_model_json`/`repair_unescaped_quotes`) yang terbukti menuntaskan
buku utuh Claude pada live test 5 Okt 2026 — respons yang semula dibuang
kini terbaca, sehingga batch tidak perlu diminta ulang.

## Catatan

- Gateway yang tidak mengembalikan usage (Qwen/DeepSeek via bansosai)
  dicatat memakai estimasi lokal; watchdog tetap bekerja, angkanya
  pendekatan, bukan angka dashboard.
- Model gateway lain yang tidak terdaftar di tabel batas output
  mempertahankan perilaku 2.4.0 persis.
- Pemakaian: `--provider openai --oa-token-budget 1500000` membatasi satu
  sesi di 1,5 juta token dari kuota harian 2 juta.

## Verifikasi

- Uji offline dengan mock gateway: alias round-trip utuh (hasil kembali
  memakai ID asli), max_tokens 32768 terkirim, watchdog berhenti bersih.
- Identitas settings mode Gemini byte-identik antara 2.4.0 dan 2.4.1;
  diff file mesin inti (gemini_client, token_budget, pipeline, storage,
  epub, rate_limit, adaptive) bersih.
- Live test rilis ini sudah dijalankan 6 Okt 2026: buku utuh DeepSeek 3.2 via bansosai selesai 2.048/2.048 dalam 46 request; watchdog `--oa-token-budget 400000` berhenti bersih di 1.962, sisa 86 unit selesai lewat resume (budget 450K); dashboard ±430.093/2.000.000 token (~21,5%). Alias transport dan batas output 32.768 ikut terpakai tanpa masalah validasi.
