# Patch Notes v2.4.0 — Penyedia OpenAI-Compatible (Opsional)

## Fitur — penyedia kedua di samping Gemini

- Pilihan **Penyedia AI** di sidebar UI: *Gemini (Google AI Studio)* atau
  *OpenAI-compatible (gateway, mis. bansosai)*. CLI: `--provider gemini|openai`,
  `--oa-base-url` (default `https://api.bansosai.app/v1`), `--model` kini
  ber-default per provider (Gemini: `gemini-3.5-flash-lite`; OpenAI:
  `claude-sonnet-4-5`).
- Key gateway dibaca dari `--api-key` atau env `OA_API_KEY`/`OPENAI_API_KEY`;
  key Gemini dari env Gemini seperti biasa. **Kedua key tidak pernah
  tercampur** dan rotasi multi-key tetap khusus Gemini.
- Klien baru `novel_translator/openai_client.py` memakai kontrak yang sama
  persis dengan jalur Gemini: PromptBuilder yang sama (instruksi sistem +
  payload JSON unit yang sama), validasi + salvage v2.2.4 yang sama, packing,
  limiter RPM/TPM/RPD lokal, jeda/resume, dan ekspor EPUB yang sama. Yang
  berbeda hanya transport (chat completions) dan bentuk respons.
- Respons gateway dinormalisasi sebelum validasi: bungkus `{"units": [...]}`
  dibongkar, pagar ```json dibuang, kunci ekstra (`format`, `document`, ...)
  dibuang — karena model non-Gemini tidak terikat responseSchema Gemini.
  Error gateway berbentuk HTTP 200 + body `{"error": ...}` dikenali dari
  kode error-nya; 401/403/404 berhenti dengan pesan jelas, 408/429/5xx
  memakai backoff + cooldown yang sama seperti jalur Gemini.
- Klien selalu mengirim User-Agent ala browser (gateway di balik Cloudflare
  menolak User-Agent bawaan pustaka HTTP).

## Kompatibilitas cache (penting)

- `settings()` hanya menulis kunci `provider`/`oa_base_url` bila provider
  bukan `"gemini"` — pola yang sama seperti domain di 2.3.0. Settings dan
  **job_id mode Gemini byte-identik dengan 2.3.0** (terbukti pada 5
  konfigurasi: default, gaya literal, non-fiksi, model lain, glosarium),
  jadi cache 2.0.x–2.3.0 — termasuk 40.899 blok Deep Sea Embers — tetap
  terbaca dan progres berjalan tidak terganggu.
- `ENGINE_VERSION=2.0.0`, `PROMPT_VERSION=2` tidak berubah. Hasil gateway
  tinggal di namespace cache sendiri (model + provider + base URL masuk
  identitas), tidak pernah menimpa hasil Gemini.
- Seluruh file mesin (pipeline, gemini_client, epub, token_budget,
  rate_limit, storage, validator) **byte-identik** dengan 2.3.0; perubahan
  hanya di `models.py` (aditif), `cli.py`, `app.py`, dan modul baru.

## Bukti

- Offline: py_compile bersih; isolasi klien vs mock gateway lokal (respons
  gaya Claude terbungkus + kunci ekstra): 66/66 run valid; end-to-end CLI vs
  mock (termasuk error 504-di-dalam-200 lalu retry): EPUB selesai dan lolos
  pemeriksaan integritas.
- Live (bansosai, `claude-sonnet-4-5`, key uji terpisah): Pride and Prejudice
  (kutipan) selesai dalam 2 request; Origin of Species Bab I dalam 1 request;
  keduanya ekspor EPUB valid.

## Catatan kualitas & batasan

- Kualitas antar-run bervariasi seperti Gemini: pada satu sampel benchmark
  Claude memecahkan jebakan "my dear Mr. Bennet" dan "sporting plants",
  pada run pipeline lain ia jatuh di "tanaman olahraga". Untuk istilah
  jebakan yang menetap, glosarium tetap alat yang tepat.
- Gateway pihak ketiga bersifat promosi: kuota/ketersediaan bisa berubah
  atau berakhir kapan saja, dan teks buku melewati server mereka. Perlakukan
  key gateway sebagai key uji, bukan pengganti free tier Gemini.
- Model dengan batas output kecil (mis. deepseek-3.2 ±8K token) sebaiknya
  memakai `--max-output-tokens 8000` agar batch tidak terpotong; varian
  model "thinking" tidak disarankan untuk terjemahan batch.
- RPM lokal maksimum tetap 15; gateway berbayar 20 RPM belum dimanfaatkan
  penuh di versi ini.
