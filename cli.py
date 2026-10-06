from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import threading
from pathlib import Path

from novel_translator import TranslationOptions, translate_novel
from novel_translator.epub import EpubBook
from novel_translator.jobs import safe_error
from novel_translator.models import (DEFAULT_MODEL, DEFAULT_OA_BASE_URL, DEFAULT_OA_MODEL,
                                     parse_glossary, PROFILES)
from novel_translator.openai_client import OpenAICompatibleTranslator


def main(argv=None):
    parser = argparse.ArgumentParser(description="Translator EPUB Inggris ke Indonesia (Gemini atau gateway OpenAI-compatible).")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", action="store_true", help="Audit struktur tanpa API key/request")
    parser.add_argument("--provider", choices=["gemini", "openai"], default="gemini",
                        help="Penyedia AI: gemini (default) atau openai untuk gateway OpenAI-compatible "
                             "(mis. bansosai). Key Gemini tidak pernah dipakai ke gateway dan sebaliknya.")
    parser.add_argument("--oa-base-url", default=DEFAULT_OA_BASE_URL,
                        help="Base URL gateway OpenAI-compatible (provider openai)")
    parser.add_argument("--oa-token-budget", type=int, default=0,
                        help="Khusus provider openai: berhenti bersih (progres tersimpan) saat "
                             "total token sesi mencapai angka ini; 0 = mati")
    parser.add_argument("--model", default=None,
                        help="ID model; default per provider (Gemini: gemini-3.5-flash-lite, OpenAI: claude-sonnet-4-5)")
    parser.add_argument("--api-key", action="append", default=[],
                        help="Dapat diulang untuk rotasi multi-key; digabung dengan "
                             "env GEMINI_API_KEYS (koma/baris baru) dan GEMINI_API_KEY")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(os.getenv("EPUB_TRANSLATOR_WORKSPACE", str(Path(__file__).parent / "workspace"))) / "cache")
    parser.add_argument("--glossary", type=Path, help="UTF-8: Inggris = Indonesia per baris")
    parser.add_argument("--instruction", type=Path, help="File UTF-8 instruksi tambahan")
    parser.add_argument("--style", default="Natural dan mengalir seperti novel Indonesia modern")
    parser.add_argument("--domain", choices=["fiction", "nonfiction"], default="fiction",
                        help="Domain terjemahan: fiksi (default) atau non-fiksi (instruksi akurasi fakta/istilah)")
    parser.add_argument("--fresh", action="store_true",
                        help="Generate ulang dari nol: abaikan cache buku ini dan timpa output")
    parser.add_argument("--chunk-chars", type=int, default=8000, help="Kompatibilitas v2.0; diabaikan, gunakan --input-tokens")
    parser.add_argument("--mode", choices=list(PROFILES), default="Aggressive")
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--target-tpm", type=int)
    parser.add_argument("--workers", type=int, choices=[1, 2, 3, 4])
    tuning = parser.add_mutually_exclusive_group()
    tuning.add_argument("--auto-tune", action="store_true", help="Aktifkan tuning; default mati")
    tuning.add_argument("--no-auto-tune", action="store_true", help="Kompatibilitas; tuning default mati")
    parser.add_argument("--count-tokens", choices=["auto", "always", "estimate"], default="auto")
    parser.add_argument("--max-output-tokens", type=int, default=65536)
    parser.add_argument("--temperature", type=float, default=.2)
    parser.add_argument("--max-requests", type=int, default=490)
    parser.add_argument("--rpm", type=int)
    parser.add_argument("--tpm", type=int, help="Batas input token rolling 60 detik; maksimum proyek 250000")
    parser.add_argument("--rpd", type=int, help="Batas request lokal per hari Pasifik")
    parser.add_argument("--official-rpd", type=int, default=500, help="Referensi kuota harian proyek; dapat diubah")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--overwrite", action="store_true", help="Izinkan timpa output, tidak pernah sumber")
    parser.add_argument("--epubcheck-jar", type=Path, help="Opsional: EPUBCheck sebagai gate sebelum publikasi")
    parser.add_argument("--offline", action="store_true", help="Ekspor hanya jika cache sudah lengkap; tanpa key")
    args = parser.parse_args(argv)

    def split_keys(raw):
        parts = []
        for chunk in str(raw).replace(",", "\n").split("\n"):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts

    keys = []
    for supplied in (args.api_key or []):
        keys.extend(split_keys(supplied))
    if args.provider == "openai":
        # Key gateway terpisah dari key Gemini; tidak pernah tercampur.
        keys.extend(split_keys(os.getenv("OA_API_KEY", "")))
        keys.extend(split_keys(os.getenv("OPENAI_API_KEY", "")))
    else:
        keys.extend(split_keys(os.getenv("GEMINI_API_KEYS", "")))
        legacy = os.getenv("GEMINI_API_KEY", "").strip()
        if legacy:
            keys.append(legacy)
    # de-duplikasi dengan urutan tetap
    keys = list(dict.fromkeys(keys))
    event = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: event.set())
    try:
        if args.inspect:
            book = EpubBook(args.input)
            print(json.dumps({"title": book.title, "source_sha256": book.source_hash,
                              "documents": len(book.documents), "text_runs": len(book.runs),
                              "units": len(book.units), "warnings": book.warnings},
                             ensure_ascii=False, indent=2))
            return 0
        if not args.offline and not keys and sys.stdin.isatty():
            prompt = ("Gemini API key (tersembunyi): " if args.provider == "gemini"
                      else "API key gateway (tersembunyi): ")
            keys = [getpass.getpass(prompt).strip()]
            keys = [k for k in keys if k]
        preset = PROFILES[args.mode]
        def setting(name, supplied):
            return preset[name] if supplied is None else supplied
        options = TranslationOptions(
            api_keys=keys, api_key=keys[0] if keys else "",
            model=args.model or (DEFAULT_MODEL if args.provider == "gemini" else DEFAULT_OA_MODEL),
            provider=args.provider, oa_base_url=args.oa_base_url, oa_token_budget=args.oa_token_budget,
            cache_dir=args.cache_dir,
            style=args.style, domain=args.domain, ignore_cache=args.fresh, chunk_chars=args.chunk_chars, max_requests=args.max_requests,
            rpm=setting("rpm", args.rpm), tpm=setting("tpm", args.tpm), rpd=setting("rpd", args.rpd), delay_seconds=args.delay,
            target_tpm=setting("target_tpm", args.target_tpm), target_input_tokens=setting("target_input_tokens", args.input_tokens),
            max_input_tokens=setting("max_input_tokens", args.max_input_tokens), workers=setting("workers", args.workers),
            auto_tune=args.auto_tune, official_rpd=args.official_rpd, token_count_mode=args.count_tokens,
            max_output_tokens=args.max_output_tokens, temperature=args.temperature,
            max_retries=args.max_retries, timeout=args.timeout, overwrite=args.overwrite,
            epubcheck_jar=args.epubcheck_jar,
            glossary=parse_glossary(args.glossary.read_text("utf-8")) if args.glossary else {},
            custom_instruction=args.instruction.read_text("utf-8") if args.instruction else "")
        output = args.output or args.input.with_name(args.input.stem + "_Indonesia.epub")

        def progress(done, total, requests, message):
            print(f"[{done}/{total} | request {requests}] {message}", flush=True)

        factory = OpenAICompatibleTranslator if args.provider == "openai" else None
        result = translate_novel(args.input, output, options, progress=progress, cancel_event=event,
                                 **({"translator_factory": factory} if factory else {}))
        print(result.message)
        if result.complete:
            print(f"EPUB: {result.primary_output}")
            if result.report_output:
                print(f"Laporan: {result.report_output}")
            return 0
        return 3
    except Exception as exc:
        print("Gagal: " + safe_error(exc, keys), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
