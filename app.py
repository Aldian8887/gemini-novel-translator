from __future__ import annotations

import os
import re
import json
import threading
import html
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from novel_translator.jobs import BackgroundJob, safe_error
from novel_translator.models import (DEFAULT_MODEL, DEFAULT_OA_BASE_URL, DEFAULT_OA_MODEL,
                                     TranslationOptions, canonical,
                                     digest, parse_glossary, PROFILES, RPD_PRESETS)
from novel_translator.openai_client import OpenAICompatibleTranslator
from novel_translator.pipeline import translate_novel as _translate_novel
from functools import partial as _partial
from novel_translator.storage import atomic_bytes
from novel_translator.preferences import (checkpoints, load_preferences, save_preferences, stored_quota,
                                          stored_quota_limits, save_local_quota)

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("EPUB_TRANSLATOR_WORKSPACE", str(ROOT / "workspace"))).expanduser()

# Dashboard Komando — tema Ember Control (visual saja: tidak mengubah widget key,
# urutan konstruksi TranslationOptions, maupun logika job).
_EMBER_CSS = """<style>
.stApp { background-color: #141010; }
h1, h2, h3 { color: #F5F0EB !important; }
.app-subtitle { color: #A8A29E; font-size: 0.95rem; margin-top: -0.75rem; }
section[data-testid="stSidebar"] { background-color: #1C1410; }
section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }
.ember-card { background: #241D18; border: 1px solid #3A2E24; border-radius: 0.75rem;
  padding: 1rem 1.25rem; margin-bottom: 0.5rem; }
.ember-card-title { color: #F5F0EB; font-weight: 700; font-size: 1.05rem; }
.ember-keyhash { color: #A8A29E; font-family: monospace; font-size: 0.85rem; font-weight: 400; }
.ember-bar { background: #3A2E24; border-radius: 9999px; height: 0.6rem;
  margin: 0.6rem 0 0.4rem 0; overflow: hidden; }
.ember-bar-fill { background: linear-gradient(90deg, #F59E0B, #FBBF24); height: 100%;
  border-radius: 9999px; }
.ember-card-sub { color: #A8A29E; font-size: 0.85rem; }
.ember-card-warn { border-color: #92610E; background: #2A1E0E; }
.ember-card-warn .ember-card-title { color: #FBBF24; }
.ember-pill { display: inline-block; padding: 0.35rem 0.9rem; border-radius: 9999px;
  font-weight: 700; font-size: 0.85rem; letter-spacing: 0.04em; }
.ember-pill-idle { background: #292524; color: #A8A29E; border: 1px solid #44403C; }
.ember-pill-run { background: #451A03; color: #FBBF24; border: 1px solid #F59E0B;
  animation: ember-pulse 1.6s ease-in-out infinite; }
.ember-pill-done { background: #052E16; color: #4ADE80; border: 1px solid #16A34A; }
.ember-pill-err { background: #450A0A; color: #F87171; border: 1px solid #DC2626; }
@keyframes ember-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.55; } }
div[data-testid="stMetric"] { background: #241D18; border: 1px solid #3A2E24;
  border-radius: 0.75rem; padding: 0.75rem 1rem; }
div[data-testid="stMetric"] label { color: #A8A29E !important; }
div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #FBBF24 !important; }
.stProgress > div > div > div { background: linear-gradient(90deg, #F59E0B, #FBBF24) !important; }
.stProgress > div > div { border-radius: 9999px; }
.stCaption { color: #A8A29E; }
.footer-note { margin-top: 0.5rem; color: #78716C; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
button[kind="primary"] { border-radius: 0.5rem; }
div[data-testid="stExpander"] { border: 1px solid #3A2E24; border-radius: 0.75rem; }
</style>"""

_ICONS = {
    "key-round": "PHN2ZwogIGNsYXNzPSJsdWNpZGUgbHVjaWRlLWtleS1yb3VuZCIKICB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciCiAgd2lkdGg9IjI0IgogIGhlaWdodD0iMjQiCiAgdmlld0JveD0iMCAwIDI0IDI0IgogIGZpbGw9Im5vbmUiCiAgc3Ryb2tlPSIjRkJCRjI0IgogIHN0cm9rZS13aWR0aD0iMiIKICBzdHJva2UtbGluZWNhcD0icm91bmQiCiAgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIKPgogIDxwYXRoIGQ9Ik0yLjU4NiAxNy40MTRBMiAyIDAgMCAwIDIgMTguODI4VjIxYTEgMSAwIDAgMCAxIDFoM2ExIDEgMCAwIDAgMS0xdi0xYTEgMSAwIDAgMSAxLTFoMWExIDEgMCAwIDAgMS0xdi0xYTEgMSAwIDAgMSAxLTFoLjE3MmEyIDIgMCAwIDAgMS40MTQtLjU4NmwuODE0LS44MTRhNi41IDYuNSAwIDEgMC00LTR6IiAvPgogIDxjaXJjbGUgY3g9IjE2LjUiIGN5PSI3LjUiIHI9Ii41IiBmaWxsPSIjRkJCRjI0IiAvPgo8L3N2Zz4K",
    "trending-up": "PHN2ZwogIGNsYXNzPSJsdWNpZGUgbHVjaWRlLXRyZW5kaW5nLXVwIgogIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIKICB3aWR0aD0iMjQiCiAgaGVpZ2h0PSIyNCIKICB2aWV3Qm94PSIwIDAgMjQgMjQiCiAgZmlsbD0ibm9uZSIKICBzdHJva2U9IiNGQkJGMjQiCiAgc3Ryb2tlLXdpZHRoPSIyIgogIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIKICBzdHJva2UtbGluZWpvaW49InJvdW5kIgo+CiAgPHBhdGggZD0iTTE2IDdoNnY2IiAvPgogIDxwYXRoIGQ9Im0yMiA3LTguNSA4LjUtNS01TDIgMTciIC8+Cjwvc3ZnPgo=",
    "rocket": "PHN2ZwogIGNsYXNzPSJsdWNpZGUgbHVjaWRlLXJvY2tldCIKICB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciCiAgd2lkdGg9IjI0IgogIGhlaWdodD0iMjQiCiAgdmlld0JveD0iMCAwIDI0IDI0IgogIGZpbGw9Im5vbmUiCiAgc3Ryb2tlPSIjRkJCRjI0IgogIHN0cm9rZS13aWR0aD0iMiIKICBzdHJva2UtbGluZWNhcD0icm91bmQiCiAgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIKPgogIDxwYXRoIGQ9Ik0xMiAxNXY1czMuMDMtLjU1IDQtMmMxLjA4LTEuNjIgMC01IDAtNSIgLz4KICA8cGF0aCBkPSJNNC41IDE2LjVjLTEuNSAxLjI2LTIgNS0yIDVzMy43NC0uNSA1LTJjLjcxLS44NC43LTIuMTMtLjA5LTIuOTFhMi4xOCAyLjE4IDAgMCAwLTIuOTEtLjA5IiAvPgogIDxwYXRoIGQ9Ik05IDEyYTIyIDIyIDAgMCAxIDItMy45NUExMi44OCAxMi44OCAwIDAgMSAyMiAyYzAgMi43Mi0uNzggNy41LTYgMTFhMjIuNCAyMi40IDAgMCAxLTQgMnoiIC8+CiAgPHBhdGggZD0iTTkgMTJINHMuNTUtMy4wMyAyLTRjMS42Mi0xLjA4IDUgLjA1IDUgLjA1IiAvPgo8L3N2Zz4K",
    "sliders-horizontal": "PHN2ZwogIGNsYXNzPSJsdWNpZGUgbHVjaWRlLXNsaWRlcnMtaG9yaXpvbnRhbCIKICB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciCiAgd2lkdGg9IjI0IgogIGhlaWdodD0iMjQiCiAgdmlld0JveD0iMCAwIDI0IDI0IgogIGZpbGw9Im5vbmUiCiAgc3Ryb2tlPSIjRkJCRjI0IgogIHN0cm9rZS13aWR0aD0iMiIKICBzdHJva2UtbGluZWNhcD0icm91bmQiCiAgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIKPgogIDxwYXRoIGQ9Ik0xMCA1SDMiIC8+CiAgPHBhdGggZD0iTTEyIDE5SDMiIC8+CiAgPHBhdGggZD0iTTE0IDN2NCIgLz4KICA8cGF0aCBkPSJNMTYgMTd2NCIgLz4KICA8cGF0aCBkPSJNMjEgMTJoLTkiIC8+CiAgPHBhdGggZD0iTTIxIDE5aC01IiAvPgogIDxwYXRoIGQ9Ik0yMSA1aC03IiAvPgogIDxwYXRoIGQ9Ik04IDEwdjQiIC8+CiAgPHBhdGggZD0iTTggMTJIMyIgLz4KPC9zdmc+Cg==",
}

def _h3(icon, text):
    """Header seksi dengan ikon garis elegan (Lucide, ISC) + teks."""
    img = (f'<img src="data:image/svg+xml;base64,{_ICONS[icon]}" width="24" height="24" '
           f'style="vertical-align:-3px; margin-right:0.5rem;" alt="">')
    st.markdown(f"<h3>{img}{text}</h3>", unsafe_allow_html=True)

st.set_page_config(page_title="Translator EPUB", page_icon="📖", layout="wide")

if hasattr(st, "html"):
    st.html(_EMBER_CSS)
else:
    st.markdown(_EMBER_CSS, unsafe_allow_html=True)


@st.cache_resource
def job_registry():
    return {"lock": threading.Lock(), "jobs": {}}


registry = job_registry()
registry_key = str(WORKSPACE.resolve())
with registry["lock"]:
    last_job = registry["jobs"].get(registry_key)
if "job" not in st.session_state and last_job:
    st.session_state.update(last_job)
    st.session_state["restored_job"] = True

existing = st.session_state.get("job")
running = existing is not None and existing.snapshot()["running"]


def _deck_status():
    if existing is None:
        return ("SIAP", "idle")
    s = existing.snapshot()
    if s["running"]:
        return ("BERJALAN", "run")
    if s.get("error"):
        return ("GAGAL", "err")
    res = s.get("result")
    if res is not None and getattr(res, "complete", False):
        return ("SELESAI", "done")
    return ("SIAP", "idle")


_head_l, _head_r = st.columns([4, 1])
with _head_l:
    st.title("EPUB Inggris → Indonesia")
    st.markdown(
        '<p class="app-subtitle">Command deck · Jalur Gemini default · cache lokal · '
        "tanpa perubahan struktur EPUB.</p>",
        unsafe_allow_html=True,
    )
with _head_r:
    _slabel, _scls = _deck_status()
    st.markdown(
        f'<div style="text-align:right; padding-top: 1.4rem;">'
        f'<span class="ember-pill ember-pill-{_scls}">● {_slabel}</span></div>',
        unsafe_allow_html=True,
    )


def restore_settings(settings):
    chosen = settings.get("model", DEFAULT_MODEL)
    st.session_state["model_choice"] = chosen if chosen in {DEFAULT_MODEL, "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"} else "ID model lain"
    st.session_state["custom_model"] = chosen
    st.session_state["style"] = settings.get("style", "Natural dan mengalir seperti novel Indonesia modern")
    st.session_state["domain"] = settings.get("domain", "fiction")
    st.session_state["provider"] = settings.get("provider", "gemini")
    if settings.get("provider") == "openai":
        st.session_state["oa_model"] = settings.get("model", DEFAULT_OA_MODEL)
        st.session_state["oa_base_url"] = settings.get("oa_base_url", DEFAULT_OA_BASE_URL)
    st.session_state["glossary"] = "\n".join(f"{k} = {v}" for k, v in settings.get("glossary", {}).items())
    st.session_state["instruction"] = settings.get("custom_instruction", "")


if "preferences_loaded" not in st.session_state:
    restored = (json.loads(st.session_state["job_settings"])
                if st.session_state.get("restored_job") else load_preferences(WORKSPACE))
    restore_settings(restored)
    st.session_state["preferences_loaded"] = True

with st.sidebar.expander("Lanjutkan checkpoint lama"):
    saved_jobs = checkpoints(WORKSPACE)
    if saved_jobs:
        checkpoint = st.selectbox("Checkpoint tersimpan", saved_jobs,
                                  format_func=lambda x: f"{x['source'][:8]} · {x['done']}/{x['total']} · {x['settings'].get('model', '')}",
                                  disabled=running)
        if st.button("Muat pengaturan checkpoint", disabled=running):
            restore_settings(checkpoint["settings"])
            st.toast("Pengaturan checkpoint dimuat.")
            st.rerun()
        st.caption("Memulihkan model, gaya, glossary dan instruksi persis. Unggah EPUB sumber yang sama untuk melanjutkan.")
    else:
        st.caption("Checkpoint akan terlihat setelah sesi pertama. Saat update, pertahankan folder workspace lama.")

with st.sidebar:
    _h3("sliders-horizontal", "Kontrol")
    provider = st.selectbox("Penyedia AI", ["gemini", "openai"], key="provider", disabled=running,
                            format_func=lambda p: {"gemini": "Gemini (Google AI Studio)",
                                                   "openai": "OpenAI-compatible (gateway, mis. bansosai)"}[p],
                            help="Gateway OpenAI-compatible memakai key dan kuota terpisah dari Gemini; "
                                 "hasilnya tersimpan di cache terpisah dan tidak tercampur.")
    st.subheader("Kredensial & model")
    if provider == "openai":
        base_url = st.text_input("Base URL gateway", value=DEFAULT_OA_BASE_URL, key="oa_base_url",
                                 disabled=running,
                                 help="Alamat basis API berakhiran /v1, mis. https://api.bansosai.app/v1. "
                                      "Teks buku dikirim ke alamat ini.")
        key = st.text_input("API key gateway", value=os.getenv("OA_API_KEY", ""),
                            type="password", disabled=running,
                            help="Key dipakai di memori, tidak disimpan dalam cache atau laporan.")
        model = st.text_input("ID model", value=DEFAULT_OA_MODEL, key="oa_model", disabled=running,
                              help="Sesuai katalog gateway, mis. claude-sonnet-4-5 atau deepseek-3.2 "
                                   "(untuk deepseek-3.2 turunkan Maksimum output token ke 8000).")
        api_keys = [key.strip()] if key.strip() else []
        st.caption("Model gateway tidak selalu seketat Gemini soal format JSON; tool menormalisasi "
                   "respons dan memakai validasi + salvage yang sama. RPM/TPM/RPD lokal di bawah "
                   "tetap berlaku sebagai rem pengaman, bukan kuota resmi gateway.")
    else:
        base_url = DEFAULT_OA_BASE_URL
        key = st.text_input("API key", value=os.getenv("GEMINI_API_KEY", ""),
                            type="password", disabled=running,
                            help="Key dipakai di memori, tidak disimpan dalam cache atau laporan.")
        with st.expander("API key tambahan (rotasi otomatis)"):
            extra_keys_raw = st.text_area("Satu key per baris", value=os.getenv("GEMINI_API_KEYS", ""),
                                          disabled=running,
                                          help="Opsional. Saat satu key mencapai batas hariannya, tool otomatis "
                                               "beralih ke key berikutnya tanpa jeda manual. Tiap key memakai "
                                               "kuota project-nya sendiri (mis. 500 RPD).")
            st.caption("Key tambahan terlihat jelas di sini. Semua key hanya dipakai di memori.")
        api_keys = ([key.strip()] if key.strip() else []) + \
                   [k.strip() for k in extra_keys_raw.splitlines() if k.strip()]
        api_keys = list(dict.fromkeys(api_keys))
        if len(api_keys) > 1:
            st.caption(f"{len(api_keys)} key aktif — rotasi otomatis saat satu key mencapai batas harian.")
        model_choice = st.selectbox("Model", [DEFAULT_MODEL, "gemini-3.1-flash-lite",
                                             "gemini-2.5-flash-lite", "ID model lain"], key="model_choice", disabled=running)
        model = st.text_input("ID model Gemini", key="custom_model", disabled=running) if model_choice == "ID model lain" else model_choice
        if model != DEFAULT_MODEL:
            st.info("Preset ini untuk kuota proyek Gemini 3.5 Flash Lite. Sesuaikan Advanced Settings dengan kuota model pilihanmu.")
        st.caption("Batas proyek: 15 RPM · 250.000 input TPM. Referensi RPD dapat diubah di bawah.")
        st.markdown("[Cek kuota proyek](https://aistudio.google.com/usage?tab=rate-limit)")
    st.divider()
    st.subheader("Batas & kuota")
    mode = st.selectbox("MODE", list(PROFILES), index=2, disabled=running)
    preset = PROFILES[mode]
    auto_tune = st.checkbox("Auto Tune Aggressive Mode", value=False, disabled=running)
    max_requests = st.number_input("Batas request sesi", 1, 10000, 490, disabled=running,
                                   help="Seluruh percobaan, retry, dan countTokens masuk anggaran lokal.", format="%d")
    workers = st.selectbox("Concurrent request", [1, 2, 3, 4], index=preset["workers"]-1,
                          key=f"workers_{mode}", disabled=running)
    quota_limits = stored_quota_limits(WORKSPACE, model)
    rpd_key = f"rpd_limit_{model}_{mode}"
    def choose_rpd_preset():
        st.session_state[rpd_key] = RPD_PRESETS[st.session_state[f"rpd_choice_{mode}"]]
    rpd_choice = st.selectbox("RPD safety", list(RPD_PRESETS), index=0 if mode == "Safe" else 1,
                             key=f"rpd_choice_{mode}", disabled=running, on_change=choose_rpd_preset)
    with st.expander("Advanced Settings"):
        rpm = st.number_input("RPM hard", 1, 15, preset["rpm"], key=f"rpm_{mode}", disabled=running, format="%d")
        target_tpm = st.number_input("TPM target", 2000, 250000, preset["target_tpm"], step=1000,
                                     key=f"target_tpm_{mode}", disabled=running, format="%d")
        tpm = st.number_input("TPM hard", 2000, 250000, preset["tpm"], step=1000,
                              key=f"tpm_{mode}", disabled=running, format="%d")
        official_rpd = st.number_input("Official RPD Limit", min_value=1,
                                       value=quota_limits.get("official_rpd", 500), step=1,
                                       key=f"official_rpd_{model}", disabled=running, format="%d")
        rpd = st.number_input("Local RPD Safety Limit", min_value=1,
                              value=quota_limits.get("rpd", RPD_PRESETS[rpd_choice]), step=1,
                              key=rpd_key, disabled=running, format="%d")
        target_input = st.number_input("Target total input token/request", 512, 40000, preset["target_input_tokens"], step=1000,
                                      key=f"input_{mode}", disabled=running, format="%d")
        max_input = st.number_input("Maksimum input token/request", 512, 40000, preset["max_input_tokens"], step=1000,
                                   key=f"max_input_{mode}", disabled=running, format="%d")
        count_mode = st.selectbox("Penghitungan token", ["auto", "always", "estimate"], disabled=running,
                                 format_func=lambda x: {"auto": "Auto: kalibrasi awal + usage aktual", "always": "countTokens setiap batch", "estimate": "Estimasi + usage aktual"}[x])
        st.caption("Auto menghemat panggilan hitung. Estimasi mencakup seluruh prompt dan dikoreksi dari usageMetadata. "
                   "countTokens masuk RPM/TPM lokal, tetapi tidak menambah Local RPD.")
        max_output = st.number_input("Maksimum output token", 2048, 65536, 65536, disabled=running, format="%d")
        temperature = st.slider("Temperature", 0.0, 1.0, .2, .1, disabled=running)
        retry = st.number_input("Retry gangguan API/koneksi", 0, 8, 5, disabled=running, format="%d")
        timeout = st.number_input("Timeout request (detik)", 5, 900, 600, disabled=running, format="%d")
    quota_keys = api_keys if api_keys else [None]
    if len(quota_keys) > 1:
        qsel = st.selectbox("Key untuk akuntansi RPD",
                            quota_keys,
                            format_func=lambda k: f"key #{quota_keys.index(k) + 1} · {digest(k)[:8]}…",
                            key=f"rpdkey_{model}", disabled=running)
    else:
        qsel = quota_keys[0]
    used, reset_at = stored_quota(WORKSPACE, model, key=qsel)
    with st.expander("Akuntansi RPD lokal"):
        revision = st.session_state.get("rpd_edit_revision", 0)
        used_key = f"rpd_used_{model}_{used}_{revision}"
        local_used = st.number_input("Pemakaian RPD lokal saat ini", min_value=0, value=used or 0, step=1,
                                     key=used_key, disabled=running or used is None, format="%d")
        st.warning("Mengubah atau mereset RPD lokal hanya mengubah penghitung internal "
                   "translator. Kuota API resmi provider tidak ikut direset.")
        def apply_rpd_control(reset=False):
            try:
                if reset:
                    save_local_quota(WORKSPACE, model, used=0, key=qsel)
                else:
                    save_local_quota(WORKSPACE, model, used=st.session_state[used_key],
                                     official_rpd=st.session_state[f"official_rpd_{model}"],
                                     rpd=st.session_state[rpd_key], key=qsel)
                st.session_state["rpd_edit_revision"] = revision + 1
                st.session_state["rpd_control_error"] = ""
                st.toast("RPD lokal direset ke 0." if reset else "RPD lokal tersimpan.")
            except Exception as exc:
                st.session_state["rpd_control_error"] = safe_error(exc, api_keys)
        # Callbacks run before the full rerun: no early abort that would discard
        # later widgets' unsaved glossary, instructions or uploaded EPUB.
        st.button("Simpan RPD lokal", disabled=running or used is None, on_click=apply_rpd_control)
        st.button("Reset RPD lokal ke 0", disabled=running or used is None,
                  on_click=apply_rpd_control, args=(True,))
        if st.session_state.get("rpd_control_error"):
            st.error(st.session_state["rpd_control_error"])
    reset_label = datetime.fromtimestamp(reset_at, timezone.utc).strftime("%d %b %Y %H:%M UTC")
    key_note = "" if qsel is None else f" (key #{quota_keys.index(qsel) + 1})"
    st.caption(f"RPD lokal tersimpan{key_note}: {used if used is not None else 'tidak dapat dibaca'} / {rpd}. Reset: {reset_label} (00:00 Pasifik).")
    st.caption("Pemakaian aplikasi/perangkat lain tidak terlihat. Target TPM menyesuaikan kapasitas, ukuran batch dan latensi; bukan jaminan kecepatan.")
    st.divider()


def _key_cards():
    _h3("key-round", "Key Status")
    if not api_keys:
        st.markdown(
            '<div class="ember-card ember-card-warn"><div class="ember-card-title">⚠️ Belum ada API key</div>'
            '<div class="ember-card-sub">Masukkan key di panel Kontrol (sidebar) untuk mulai.</div></div>',
            unsafe_allow_html=True)
        return
    cols = st.columns(len(quota_keys))
    for i, (col, k) in enumerate(zip(cols, quota_keys)):
        with col:
            if k is None:
                st.markdown(
                    '<div class="ember-card"><div class="ember-card-title">Tanpa key</div>'
                    '<div class="ember-card-sub">Masukkan API key untuk mengaktifkan.</div></div>',
                    unsafe_allow_html=True)
                continue
            ku, kr = stored_quota(WORKSPACE, model, key=k)
            ku = ku or 0
            pct = min(1.0, ku / rpd) if rpd else 0
            rlab = datetime.fromtimestamp(kr, timezone.utc).strftime("%d %b %H:%M UTC")
            active = " · ⚡ aktif" if k == qsel else ""
            st.markdown(
                f'<div class="ember-card">'
                f'<div class="ember-card-title">KEY #{i + 1}{active} '
                f'<span class="ember-keyhash">{digest(k)[:8]}…</span></div>'
                f'<div class="ember-bar"><div class="ember-bar-fill" style="width:{pct * 100:.0f}%"></div></div>'
                f'<div class="ember-card-sub">RPD {ku}/{rpd} · reset {rlab}</div>'
                f'</div>',
                unsafe_allow_html=True)


def _mission_stats():
    _h3("trending-up", "Mission Stats")
    job = st.session_state.get("job")
    snap = job.snapshot() if job else None
    m = (snap.get("metrics") or {}) if snap else {}

    def _num(v):
        return f"{v:,.0f}" if v is not None else "—"

    c1, c2, c3, c4 = st.columns(4)
    if m:
        c1.metric("Request sesi", _num(snap.get("requests")))
        _tpm = m.get("tpm"); _tpm_h = m.get("tpm_hard")
        c2.metric("Rolling TPM",
                  f"{_tpm / 1000:.1f}K / {_tpm_h / 1000:.1f}K" if _tpm and _tpm_h else "—")
        c3.metric("RPD tersisa", _num(m.get("rpd_remaining")))
        _eta = m.get("eta_seconds")
        c4.metric("ETA", "⏳ menunggu" if _eta is None else f"~{_eta / 60:,.1f} mnt")
    else:
        c1.metric("Request sesi", "—")
        c2.metric("Rolling TPM", "—")
        _rem = (rpd - (used or 0)) if (rpd and used is not None) else None
        c3.metric("RPD tersisa", _num(_rem))
        c4.metric("ETA", "—")


_key_cards()
_mission_stats()

_h3("rocket", "Command Deck")
with st.expander("Gaya dan istilah", expanded=False):
    domain = st.radio("Domain terjemahan", ["fiction", "nonfiction"], key="domain", disabled=running,
                      format_func=lambda x: {"fiction": "Fiksi — novel & cerita",
                                             "nonfiction": "Non-fiksi — esai, sejarah, sains, biografi"}[x],
                      help="Non-fiksi memakai instruksi khusus: akurasi fakta, angka, dan istilah teknis "
                           "diutamakan di atas gaya sastra. Hasil fiksi dan non-fiksi tersimpan di cache terpisah.")
    styles = [
        "Natural dan mengalir seperti novel Indonesia modern",
        "Akurat dan relatif literal, tetapi tetap alami",
        "Sastra, atmosferis, dan elegan",
        "Akurat, jernih, dan lugas seperti buku nonfiksi",
        "Formal dan akademis",
        "Populer, ringan, dan komunikatif"]
    if st.session_state["style"] not in styles:
        styles.append(st.session_state["style"])
    style = st.selectbox("Gaya terjemahan", styles, key="style", disabled=running)
    glossary_raw = st.text_area("Glosarium", placeholder="Beyonder = Beyonder\nSequence = Sequence",
                                key="glossary", disabled=running)
    instruction = st.text_area("Instruksi tambahan", placeholder="Gunakan aku/kau untuk dialog akrab.",
                               key="instruction", disabled=running)

uploaded = st.file_uploader("Pilih EPUB", type=["epub"], disabled=running)
st.caption("Heading, bold/italic, CSS, gambar, footnote, dan tautan dipertahankan. "
           "File sumber tetap utuh. Teks buku dikirim ke "
           f"{'Gemini' if provider == 'gemini' else 'gateway OpenAI-compatible'} untuk diterjemahkan.")
overwrite = st.checkbox("Izinkan mengganti hasil sebelumnya untuk buku dan pengaturan ini",
                        value=False, disabled=running)
regen = st.checkbox("Generate ulang dari nol (abaikan cache buku ini)", value=False, disabled=running,
                    help="Semua potongan diterjemahkan ulang walau buku dan pengaturan ini sudah pernah "
                         "selesai, dan hasil lama ditimpa. Berguna untuk membandingkan kualitas antar "
                         "domain/gaya/model. Request tetap dihitung ke kuota.")

selection = st.session_state.get("job_source") if st.session_state.get("restored_job") else None
data = None
if uploaded is not None:
    st.session_state["restored_job"] = False
    data = uploaded.getvalue()
    selection = digest(data)
    st.caption(f"`{uploaded.name}` · {len(data) / 1024 / 1024:.2f} MiB")

if st.button("Mulai / lanjutkan", type="primary", disabled=running or uploaded is None):
    try:
        if len(data) > 150 * 1024 * 1024:
            raise ValueError("Ukuran maksimal EPUB adalah 150 MiB.")
        options = TranslationOptions(api_keys=api_keys, api_key=api_keys[0] if api_keys else "",
                                     model=model, provider=provider, oa_base_url=base_url,
                                     style=style, domain=domain, ignore_cache=regen,
                                     glossary=parse_glossary(glossary_raw), custom_instruction=instruction,
                                     max_requests=max_requests, rpm=rpm, tpm=tpm, rpd=rpd,
                                     official_rpd=official_rpd, target_tpm=target_tpm,
                                     target_input_tokens=target_input, max_input_tokens=max_input, workers=workers,
                                     auto_tune=auto_tune, token_count_mode=count_mode, max_output_tokens=max_output,
                                     temperature=temperature,
                                     max_retries=retry, timeout=timeout, overwrite=overwrite,
                                     cache_dir=WORKSPACE / "cache")
        options.validate()
        settings_id = digest(canonical(options.settings()))[:10]
        source = WORKSPACE / "uploads" / f"{selection}.epub"
        if not source.exists():
            atomic_bytes(source, data)
        elif digest(source.read_bytes()) != selection:
            raise ValueError("Salinan unggahan berubah. Pindahkan salinan tersebut lalu unggah ulang.")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(uploaded.name).stem).strip("_")[:60] or "novel"
        output = WORKSPACE / "outputs" / f"book_{stem}_Indonesia_{selection[:10]}_{settings_id}.epub"
        with registry["lock"]:
            other = registry["jobs"].get(registry_key)
            if other and other["job"].snapshot()["running"]:
                raise ValueError("Masih ada terjemahan aktif pada workspace ini. Muat ulang untuk melihat progresnya.")
            save_local_quota(WORKSPACE, model, official_rpd=official_rpd, rpd=rpd)
            save_preferences(WORKSPACE, options)
            st.session_state["job_source"] = selection
            st.session_state["job_settings"] = canonical(options.settings())
            runner = (_partial(_translate_novel, translator_factory=OpenAICompatibleTranslator)
                      if provider == "openai" else _translate_novel)
            st.session_state["job"] = BackgroundJob(source, output, options, runner=runner)
            registry["jobs"][registry_key] = {k: st.session_state[k] for k in ("job", "job_source", "job_settings")}
        st.toast("Job dimulai — progres tersimpan otomatis.")
        st.rerun()
    except Exception as exc:
        st.error(safe_error(exc, api_keys))


def performance_panel(m):
    if not m:
        return
    def num(value):
        return f"{value:,.0f}" if value is not None else "—"
    def compact(value):
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "K" if value >= 1000 else str(value)
    first = st.columns(3)
    first[0].metric("Rolling TPM", f"{compact(m['tpm'])} / {compact(m['tpm_hard'])}")
    first[1].metric("RPM", f"{m['rpm']} / {m['rpm_limit']}")
    first[2].metric("RPD lokal", f"{m['rpd_used']} / {m['rpd_limit']}")
    second = st.columns(3)
    second[0].metric("Rata-rata input/request", num(m["avg_input_tokens"]))
    second[1].metric("Rata-rata output/request", num(m["avg_output_tokens"]))
    second[2].metric("Workers aktif", f"{m['workers_active']} / {m['workers_limit']}")
    st.caption(f"TPM target adaptif: {num(m['target_tpm'])} · Target batch: {num(m['target_batch_tokens'])} · "
               f"Sukses: {m['successful']} · 429: {m['errors_429']} · Retry: {m['retries']} · "
               f"countTokens: {m['count_tokens_calls']} · RPD tersisa: {m['rpd_remaining']}")
    st.caption(f"Key aktif: {m.get('active_key', '—')} · Key berkuota: {m.get('keys_available', '—')}/{m.get('keys_total', '—')}")
    st.caption(f"Reset kuota: {datetime.fromtimestamp(m['reset_at'], timezone.utc):%d %b %Y %H:%M UTC} (00:00 Pasifik). "
               "Rolling TPM memakai reservasi estimasi, kemudian diganti token aktual dari API.")
    with st.expander("Throughput dan perkiraan penyelesaian", expanded=True):
        eta = m["eta_seconds"]
        eta_label = "menunggu hasil pertama" if eta is None else f"~{eta / 60:,.1f} menit waktu aktif"
        st.write(f"Teks sumber selesai: **{num(m['translated_characters'])} karakter** · **{num(m['translated_words'])} kata** · "
                 f"**≈{num(m['translated_source_tokens'])} token sumber**")
        st.write(f"Throughput sesi: ≈{num(m['tokens_per_minute'])} token sumber/menit · {num(m['characters_per_minute'])} karakter/menit")
        st.write(f"Sisa request: ≈{m['estimated_requests_remaining']} · Kebutuhan RPD tersisa termasuk perkiraan retry: "
                 f"≈{m['estimated_rpd_needed']} · ETA: {eta_label}")
        st.caption("Token sumber adalah estimasi lokal; input/output API termasuk overhead prompt/JSON. "
                   "ETA belum memasukkan jeda antarhari ketika RPD habis.")
    if m["rpd_bottleneck"]:
        st.warning("RPD adalah bottleneck saat ini. Menaikkan RPM tidak menambah kapasitas "
                   "harian — naikkan efisiensi packing token.")


@st.fragment(run_every=1.0 if st.session_state.get("job") and st.session_state["job"].snapshot()["running"] else None)
def job_panel():
    job = st.session_state.get("job")
    if job is None:
        return
    state = job.snapshot()
    # Never offer stale files as the result of a different selection/settings.
    try:
        current = TranslationOptions(model=model, style=style, glossary=parse_glossary(glossary_raw),
                                     custom_instruction=instruction,
                                     domain=st.session_state.get("domain", "fiction"),
                                     provider=st.session_state.get("provider", "gemini"),
                                     oa_base_url=st.session_state.get("oa_base_url", DEFAULT_OA_BASE_URL))
        current.validate()
        matches = selection == st.session_state.get("job_source") and (
            canonical(current.settings()) == st.session_state.get("job_settings"))
    except ValueError:
        matches = False
    if not matches and not state["running"]:
        return
    pct = state["done"] / state["total"] if state["total"] else 0
    st.progress(pct, text=f"{pct * 100:.0f}% · {state['message']}")
    st.markdown(f'<span aria-live="polite" class="sr-only">{html.escape(state["message"])}</span>',
                unsafe_allow_html=True)
    st.caption(f"Tersimpan: {state['done']}/{state['total']} · Request sesi: {state['requests']}")
    performance_panel(state.get("metrics", {}))
    if state["running"]:
        if st.button("Jeda setelah request aktif", disabled=job.cancel_event.is_set()):
            job.pause()
            st.toast("Jeda diminta — request aktif diselesaikan dulu.")
        return
    # Finish a full rerun once so disabled controls unlock and polling stops.
    if st.session_state.get("finished_job") is not job:
        st.session_state["finished_job"] = job
        st.rerun()
    if state["error"]:
        st.error(state["error"])
        st.info("Progres sebelumnya tersimpan. Atasi masalah lalu klik Mulai / lanjutkan.")
    result = state["result"]
    if result is not None:
        if result.complete:
            st.success("EPUB selesai dan telah diperiksa.")
            path = result.primary_output
            if path and path.exists():
                st.download_button("Unduh EPUB Indonesia", path.read_bytes(),
                                   file_name=path.name, mime="application/epub+zip", type="primary")
                st.caption(f"Hasil juga tersedia di workspace/outputs/{path.name}")
            if result.report_output and result.report_output.exists():
                st.download_button("Unduh laporan integritas", result.report_output.read_bytes(),
                                   file_name=result.report_output.name, mime="application/json")
        else:
            st.warning(result.message)
            st.info("Pilih file dan gaya/model yang sama untuk melanjutkan. Batas kuota dan ukuran batch boleh diubah.")


job_panel()
st.divider()
st.markdown(
    '<p class="footer-note">Versi 2.4.1 · Aplikasi lokal. Pemeriksaan format tidak menjamin akurasi bahasa; '
    "periksa sampel hasil dan istilah penting.</p>",
    unsafe_allow_html=True,
)
