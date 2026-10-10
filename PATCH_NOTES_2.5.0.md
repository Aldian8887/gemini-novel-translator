# Patch Notes — v2.5.0 (10 Okt 2026)

## Book Memory v1 — Autonomous Glossary (stable baseline)

Fitur utama: glossary otomatis yang belajar kronologis antar-bab.

### Cara kerja
- Setiap bab diterjemahkan dengan glossary = manual + istilah auto-approved
  dari bab-bab sebelumnya (publication barrier deterministik).
- Setelah satu bab selesai: AI Discovery → Validasi Lokal R1–R8 (gratis) →
  AI Verification → Approval Router → tersimpan ke Book Memory.
- Worker paralel tetap di dalam bab; antar-bab sekuensial.

### Kebijakan approval
- **Review First** (default): semua kandidat menunggu persetujuan manusia.
- **Auto Safe** (opt-in): hanya nama diri person/place yang lolos seluruh
  pemeriksaan (R1–R8 + AI verified + tanpa konflik + tanpa saran koreksi).
- Manual Approved Glossary selalu menang (termasuk di tahap discovery).
- Koreksi AI tidak pernah diterapkan otomatis.

### Keamanan
- R7: proper noun yang diterjemahkan (bukan dipertahankan) → review manusia.
- R8: istilah harus muncul dalam kutipan buktinya sendiri (anti bukti tempel).
- Spoiler-safe fail-closed: default hanya bab 1; "tampil semua" eksplisit.
- Ekspor glossary UI default aman; ekspor lengkap via pilihan eksplisit.
- Cache per-bab mencakup hash glossary; perubahan glossary tidak diam-diam
  memakai cache lama.

### Kompatibilitas
- Default translator (tanpa Auto Glossary) berperilaku identik dengan v2.4.1.
- ENGINE_VERSION 2.0.0, PROMPT_VERSION 2 tidak berubah.
- Cache/settings lama byte-identik bila fitur v1 tidak diaktifkan.

### Pengujian
- 66/66 pytest (55 bawaan + 11 regresi audit).
- E2E kronologis + live test 5 bab VHD7 via API nyata (propagasi 26/26,
  compliance 100%, 0 kebocoran).

### Catatan
- Translation Skills tetap OFF/parkir (tidak aktif default).
- Biaya discovery+verifikasi aktual: ±13K token/bab.
