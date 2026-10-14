from __future__ import annotations

import os
import sqlite3
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from .adaptive import AdaptiveController
from .diagnostics import FailureLogger
from .epub import EpubBook
from .errors import CacheError, InvalidEpub, InvalidResponse, Paused, RepackRequired
from .gemini_client import GeminiTranslator
from .openai_client import OpenAICompatibleTranslator
from .metrics import ProgressMetrics
from .token_budget import PromptBuilder, TokenEstimator, TokenPacker, split_batch, text_tokens
from .models import (TranslationOptions, TranslationResult, canonical, digest,
                     validate_text)
from .rate_limit import RateLimiter
from .storage import TranslationCache, atomic_bytes, check_cancel, workspace_lock


def _inheritance_source(prev, ch_job_id, config_id):
    """Tentukan namespace lama yang boleh diwariskan, atau None.

    v2.6.0 guard isolasi: warisan HANYA bila namespace berubah karena glossary
    (config_id non-glossary identik). Bila model/style/domain/skill/provider
    berubah, config_id ikut berubah -> retranslate penuh (kontrak pipeline:
    "ganti style/config tetap menerjemahkan ulang").
    """
    if not prev:
        return None
    if prev.get("job_id") == ch_job_id:
        return None  # namespace sama -> jalur load normal
    if prev.get("config_id") != config_id:
        return None  # config berubah -> jangan wariskan antar-model/style
    return prev.get("job_id")


def translate_novel(input_path: Path, output_base: Path, options: TranslationOptions,
                    progress=None, translator_factory=GeminiTranslator, cancel_event=None, metrics=None,
                    glossary_ai_factory=None):
    """Terjemahkan EPUB. Bila options.auto_glossary aktif: mode kronologis v1.

    glossary_ai_factory: injeksi untuk pengujian — callable(options, limiter,
    cancel_event, status) -> objek dengan .discover()/.verify(). Default:
    GlossaryAIClient (API nyata).
    """
    # v2.5.2: pilih factory otomatis berdasar provider bila pemanggil memakai
    # default (GeminiTranslator) tanpa injeksi manual — mencegah translator
    # salah sasaran endpoint saat options.provider == "openai".
    if (translator_factory is GeminiTranslator
            and getattr(options, "provider", "gemini") == "openai"):
        translator_factory = OpenAICompatibleTranslator
    metrics = metrics or getattr(progress, "metrics", None)
    options.validate()
    source = Path(input_path).resolve()
    if source.suffix.lower() != ".epub":
        raise ValueError("Versi 2 khusus EPUB. Masukkan file .epub.")
    output = Path(output_base).resolve()
    if output.suffix.lower() != ".epub":
        output = Path(str(output) + ".epub")
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("Output tidak boleh menimpa EPUB sumber.")
    if output.exists() and not (options.overwrite or options.ignore_cache):
        raise FileExistsError("Output sudah ada. Pilih nama lain atau aktifkan timpa output.")
    book = EpubBook(source)
    if not book.units:
        raise InvalidEpub("Tidak ada teks Inggris yang dapat diterjemahkan; EPUB mungkin sudah berbahasa Indonesia.")
    settings = options.settings()
    job_id = digest(book.source_hash + canonical(settings))
    # v2.6.0: identitas config NON-glossary untuk guard selective re-translation.
    # Berubah saat model/style/domain/skill/provider berubah (wajib retranslate
    # penuh); stabil saat hanya glossary yang berubah (warisan diizinkan).
    config_id = digest(canonical(
        {k: v for k, v in settings.items() if k != "glossary"}))
    root = Path(options.cache_dir or source.parent / ".novel_translator_cache").resolve()
    runs = {r.id: r for r in book.runs}
    fingerprints = {}
    for unit in book.units:
        unit_hash = digest(canonical(unit.payload()))
        fingerprints.update({run.id: digest(unit_hash + canonical(run.payload())) for run in unit.runs})
    total, done, requests = len(runs), 0, 0

    def notify(message):
        if progress:
            progress(done, total, limiter.requests_made if limiter else requests, message)

    limiter = None
    try:
        with workspace_lock(root / "workspace.lock"), TranslationCache(root / "translations.sqlite3") as cache:
            # v2.3.0: mode generate ulang mengabaikan cache terjemahan lama
            # (hasil baru tetap ditulis menimpa baris yang sama), agar buku
            # yang sama bisa dibandingkan antar pengaturan/domain.
            #
            # Book Memory v1: mode auto_glossary memakai namespace cache PER BAB
            # (chapter_job_id) agar glossary kronologis deterministik. Bab yang
            # sudah published dimuat dari cache babnya masing-masing (resume).
            _v1 = None
            if options.auto_glossary:
                from .book_memory import (BookMemoryStore, compute_book_id,
                                           glossary_fingerprint,
                                           split_inherited_units)
                from . import glossary_auto as _ga
                _v1_store = BookMemoryStore()
                _v1_book_id = compute_book_id(source)
                _v1_mem = _v1_store.load(_v1_book_id)
                if not _v1_mem.title:
                    _v1_mem.title = book.title
                    _v1_store.save(_v1_mem)
                from types import SimpleNamespace
                _v1 = SimpleNamespace(store=_v1_store, book_id=_v1_book_id,
                                      mem=_v1_mem, ga=_ga,
                                      chapters=_ga.group_chapters(book))
            if _v1 is None:
                saved = {} if options.ignore_cache else cache.load(job_id, fingerprints)
            else:
                saved = {}
                if not options.ignore_cache:
                    for _ch_idx, _ch_units in _v1.chapters:
                        # Namespace memakai glossary efektif (manual menang);
                        # job_id basis sudah membedakan config, hash glossary
                        # membedakan perubahan glossary. Tidak memakai
                        # translation_progress sebagai gate: kecocokan ditentukan
                        # oleh namespace itu sendiri.
                        _ch_gl = _ga.effective_chapter_glossary(
                            _v1.mem, _ch_idx, options.glossary)
                        _ch_fps = {r.id: fingerprints[r.id]
                                   for u in _ch_units for r in u.runs}
                        saved.update(cache.load(
                            _v1.ga.chapter_job_id(job_id, _ch_idx, _ch_gl), _ch_fps))
            targets = {}
            invalid_cache = 0
            for rid, text in saved.items():
                try:
                    targets[rid] = validate_text(runs[rid], text)
                except InvalidResponse:
                    invalid_cache += 1
            done = len(targets)
            cache.state(job_id, book.source_hash, settings, "running", done, total)
            if options.ignore_cache:
                notify("Mode generate ulang: cache sebelumnya diabaikan; seluruh potongan diterjemahkan dari nol.")
            notify(f"Progres tersimpan: {done}/{total} potongan teks.")
            if invalid_cache:
                notify(f"{invalid_cache} entri cache tidak valid akan diterjemahkan ulang.")
            pending = [u.subset([r for r in u.runs if r.id not in targets])
                       for u in book.units if any(r.id not in targets for r in u.runs)]
            controller = AdaptiveController(options, cache)
            limiter = RateLimiter(cache, options, cancel_event, notify, controller=controller)
            meter = ProgressMetrics(runs, targets)
            # v2.2.1 diagnostic: append-only failure log for debugging retry
            # causes. Records error messages, batch sizes and unit IDs only --
            # never response bodies or API keys.
            diag = FailureLogger(root.parent / "diagnostics" / "failures.jsonl",
                                 job=job_id[:8], title=book.title)
            limiter.diag = diag  # reachable from GeminiTranslator for HTTP-level logging

            def emit_metrics():
                if metrics:
                    try:
                        metrics(meter.snapshot(limiter, controller))
                    except (CacheError, sqlite3.Error, OSError):
                        pass  # monitoring must not hide an already-published EPUB

            emit_metrics()
            translator = None
            v1_translators: list = []  # translator per bab (mode v1) untuk ditutup
            try:
                def _new_translator(use_options):
                    tr = translator_factory(use_options, limiter, cancel_event=cancel_event,
                                            status=notify)
                    v1_translators.append(tr)
                    return tr

                if pending and _v1 is None:
                    check_cancel(cancel_event)
                    translator = _new_translator(options)

                def validate_batch(batch, translator):
                    # v2.2.4 salvage: terima potongan valid dari respons yang
                    # tidak sempurna; hanya potongan rusak/hilang yang diminta
                    # ulang. Translator tanpa API parsial (fake/factory lama)
                    # diperlakukan all-or-nothing seperti sebelumnya.
                    partial_fn = getattr(translator, "translate_batch_partial", None)
                    if partial_fn is None:
                        translated = translator.translate_batch(batch)
                        expected = {r.id: r for u in batch for r in u.runs}
                        if not isinstance(translated, dict) or set(translated) != set(expected):
                            raise InvalidResponse("Respons batch tidak lengkap.", same_batch_retry=True)
                        return ({rid: validate_text(expected[rid], value) for rid, value in translated.items()},
                                [], [])
                    good, bad_ids, issues = partial_fn(batch)
                    expected = {r.id: r for u in batch for r in u.runs}
                    # G1 (T-GEMINI-2): run id respons yang tidak dikenal
                    # dicatat dulu ke diagnostics sebelum difilter diam-diam.
                    unknown = [rid for rid in good if rid not in expected]
                    if unknown:
                        diag.log("unknown_run_ids", units=len(batch), runs=len(unknown),
                                 sample_ids=unknown[:5], extra={"count": len(unknown)})
                    good = {rid: validate_text(expected[rid], value)
                            for rid, value in good.items() if rid in expected}
                    bad_units = []
                    for unit in batch:
                        bad_runs = [r for r in unit.runs if r.id in bad_ids]
                        if bad_runs:
                            bad_units.append(unit.subset(bad_runs))
                    return good, bad_units, issues

                def make_commit(use_job_id):
                    def commit(translated):
                        nonlocal done
                        # Only coordinator writes translations; each successful
                        # response gets a durable transaction before any UI progress.
                        if set(translated).intersection(targets):
                            raise InvalidResponse("Scheduler mencoba menimpa potongan yang sudah tersimpan.")
                        cache.save_batch(use_job_id, fingerprints, translated)
                        targets.update(translated)
                        done = len(targets)
                        meter.commit(translated)
                        cache.state(use_job_id, book.source_hash, settings, "running", done, total)
                        notify(f"Tersimpan {done}/{total} potongan teks.")
                    return commit

                def drain(work_units, packer, translator, commit):
                    """Work loop yang diekstrak: habiskan work_units via workers.

                    Dipakai jalur legacy (sekali jalan) dan jalur v1 (per bab).
                    Publication barrier v1 berjalan di koordinator antar-bab,
                    sehingga worker tidak pernah melintasi batas bab.
                    """
                    pending_q, recoveries = deque(work_units), deque()
                    failure = None
                    active = {}
                    retried = set()  # frozenset id-run yang sudah memakai jatah 1x retry urutan-dibalik
                    with ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="gemini") as pool:
                        while pending_q or recoveries or active:
                            if cancel_event is not None and cancel_event.is_set() and failure is None:
                                failure = Paused("Dijeda oleh pengguna. Seluruh hasil valid request aktif disimpan.")
                                limiter.halt(str(failure))
                            while failure is None and len(active) < controller.snapshot()["workers_limit"] and (pending_q or recoveries):
                                try:
                                    batch = recoveries.popleft() if recoveries else packer.take(pending_q)
                                    active[pool.submit(validate_batch, batch, translator)] = batch
                                except Exception as exc:
                                    failure = exc
                                    limiter.halt(str(exc))
                            if not active:
                                break
                            completed, _ = wait(active, timeout=.25, return_when=FIRST_COMPLETED)
                            for future in completed:
                                batch = active.pop(future)
                                try:
                                    good, bad_units, issues = future.result()
                                except InvalidResponse as exc:
                                    if not isinstance(exc, RepackRequired):
                                        controller.bad_response()
                                    diag.log("repack" if isinstance(exc, RepackRequired) else "invalid_response",
                                             units=len(batch),
                                             runs=sum(len(u.runs) for u in batch),
                                             est_tokens=sum(text_tokens(r.text) for u in batch for r in u.runs),
                                             error=exc,
                                             sample_ids=[r.id for u in batch for r in u.runs])
                                    if (failure is None and not isinstance(exc, RepackRequired)
                                            and getattr(exc, "same_batch_retry", False)
                                            and frozenset(r.id for u in batch for r in u.runs) not in retried):
                                        # v2.2.3: satu kali pengulangan dengan urutan unit
                                        # DIBALIK untuk kesalahan mekanis. Log live v2.2.2
                                        # membuktikan pengulangan identik gagal 8/8 (model
                                        # nyaris deterministik di temperature 0,2); membalik
                                        # urutan memecah determinisme itu tanpa mengubah
                                        # isi, ID, atau validasi. Gagal lagi -> dibelah.
                                        retried.add(frozenset(r.id for u in batch for r in u.runs))
                                        controller.retry()
                                        flipped = list(reversed(batch))
                                        active[pool.submit(validate_batch, flipped, translator)] = flipped
                                        notify("Respons tidak valid (kesalahan mekanis); mengulang batch dengan urutan unit dibalik sebelum dibelah.")
                                    else:
                                        parts = split_batch(batch)
                                        if parts and failure is None:
                                            if not isinstance(exc, RepackRequired):
                                                controller.retry()
                                            # Depth-first halves; never repeat an invalid
                                            # large request or discard an earlier commit.
                                            recoveries.extendleft(reversed(parts))
                                            notify("Respons/budget tidak lengkap; memecah batch tanpa mengubah ID cache atau struktur EPUB.")
                                        elif failure is None:
                                            failure = exc
                                            limiter.halt(str(exc))
                                except Exception as exc:
                                    if failure is None:
                                        failure = exc
                                        limiter.halt(str(exc))
                                else:
                                    try:
                                        if good:
                                            commit(good)
                                    except Exception as exc:
                                        failure = exc
                                        limiter.halt(str(exc))
                                    if bad_units and failure is None:
                                        # v2.2.4 salvage: potongan valid sudah
                                        # disimpan di atas; sisa rusak menjadi
                                        # satu batch susulan kecil. Bila nol
                                        # potongan terselamatkan, perlakukan
                                        # seperti kegagalan mekanis lama:
                                        # balik sekali, lalu belah. Setiap
                                        # putaran yang menyimpan >=1 potongan
                                        # adalah progres, jadi loop pasti
                                        # berhenti (tanpa progres -> belah ->
                                        # unit tunggal -> gagal seperti dulu).
                                        bad_runs_n = sum(len(u.runs) for u in bad_units)
                                        total_runs_n = sum(len(u.runs) for u in batch)
                                        if good:
                                            controller.retry()
                                            if bad_runs_n * 2 >= total_runs_n:
                                                controller.bad_response()
                                            diag.log("salvage", units=len(bad_units), runs=bad_runs_n,
                                                     est_tokens=sum(text_tokens(r.text) for u in bad_units for r in u.runs),
                                                     error="; ".join(issues) or "Respons sebagian tidak valid.",
                                                     sample_ids=[r.id for u in bad_units for r in u.runs],
                                                     extra={"salvaged_runs": len(good), "batch_runs": total_runs_n})
                                            recoveries.appendleft(bad_units)
                                            notify(f"Respons sebagian valid: {len(good)} potongan disimpan; "
                                                   f"{bad_runs_n} potongan bermasalah diminta ulang dalam batch susulan kecil.")
                                        else:
                                            controller.bad_response()
                                            diag.log("invalid_response", units=len(batch), runs=total_runs_n,
                                                     est_tokens=sum(text_tokens(r.text) for u in batch for r in u.runs),
                                                     error="; ".join(issues) or "Respons batch tidak lengkap.",
                                                     sample_ids=[r.id for u in batch for r in u.runs])
                                            key = frozenset(r.id for u in bad_units for r in u.runs)
                                            if key not in retried:
                                                retried.add(key)
                                                controller.retry()
                                                flipped = list(reversed(bad_units))
                                                active[pool.submit(validate_batch, flipped, translator)] = flipped
                                                notify("Respons tidak valid (kesalahan mekanis); mengulang sisa batch dengan urutan unit dibalik sebelum dibelah.")
                                            else:
                                                parts = split_batch(bad_units)
                                                if parts:
                                                    controller.retry()
                                                    recoveries.extendleft(reversed(parts))
                                                    notify("Respons/budget tidak lengkap; memecah sisa batch tanpa mengubah ID cache atau struktur EPUB.")
                                                else:
                                                    failure = InvalidResponse(issues[0] if issues else "Respons batch tidak lengkap.")
                                                    limiter.halt(str(failure))
                            emit_metrics()
                    # All in-flight futures have drained and their valid responses
                    # were committed even if another worker paused/failed first.
                    if failure is not None:
                        raise failure

                def _run_chapters_v1():
                    """Loop kronologis per bab + publication barrier.

                    Worker paralel hanya di DALAM bab. Antar-bab, koordinator
                    menjalankan discovery -> validasi -> verifikasi -> approval
                    sebelum bab berikutnya dimulai (deterministik).
                    """
                    nonlocal done
                    from dataclasses import replace as _replace
                    ga = _v1.ga
                    mem, store = _v1.mem, _v1.store
                    _ai_factory = glossary_ai_factory or ga.GlossaryAIClient
                    ai_client = _ai_factory(options, limiter, cancel_event, notify)
                    n_chapters = len(_v1.chapters)
                    # Auto Safe mensyaratkan AI verification; tanpanya paksa review_first.
                    policy = (options.approval_policy if options.auto_verify
                              else "review_first")
                    for ch_idx, ch_units in _v1.chapters:
                        check_cancel(cancel_event)
                        # Glossary kronologis: auto-approved dari bab < ch_idx,
                        # lalu MANUAL selalu menang (termasuk normalisasi kapital).
                        ch_glossary = ga.effective_chapter_glossary(
                            mem, ch_idx, options.glossary)
                        ch_options = _replace(options, glossary=ch_glossary)
                        # Namespace cache mencakup hash glossary efektif (P1):
                        # perubahan glossary -> namespace baru, tidak diam-diam
                        # memakai cache lama yang tidak kompatibel.
                        ch_job_id = ga.chapter_job_id(job_id, ch_idx, ch_glossary)
                        ch_fps = {r.id: fingerprints[r.id]
                                  for u in ch_units for r in u.runs}
                        # Muat cache job-ini (kecuali ignore_cache). Kelengkapan
                        # ditentukan oleh cache job, BUKAN progress global —
                        # sehingga ganti style/config tetap menerjemahkan ulang.
                        if not options.ignore_cache:
                            for rid, text in cache.load(ch_job_id, ch_fps).items():
                                try:
                                    targets[rid] = validate_text(runs[rid], text)
                                except InvalidResponse:
                                    pass
                            # v2.6.0 selective re-translation: bila namespace bab
                            # berubah HANYA karena glossary (guard config_id),
                            # wariskan terjemahan unit tak-terdampak dari
                            # namespace lama — API hanya mengulang unit yang
                            # benar-benar mengandung istilah berubah.
                            _prev = (mem.chapter_namespaces or {}).get(str(ch_idx))
                            _src_job = _inheritance_source(_prev, ch_job_id, config_id)
                            if _src_job:
                                _old_targets = cache.load(_src_job, ch_fps)
                                # v2.6.0: bedakan "snapshot hilang" (None) dari
                                # "glossary lama memang kosong" ({} valid).
                                _gh = (_prev or {}).get("ghash")
                                _old_glossary = (mem.glossary_snapshots or {}).get(_gh) \
                                    if _gh else None
                                if _old_targets and _old_glossary is not None:
                                    _inh, _aff_runs = split_inherited_units(
                                        _old_glossary, ch_glossary, ch_units,
                                        _old_targets)
                                    _valid = {}
                                    for _rid, _txt in _inh.items():
                                        if _rid in targets:
                                            continue  # jangan timpa (guard InvalidResponse)
                                        try:
                                            targets[_rid] = validate_text(
                                                runs[_rid], _txt)
                                            _valid[_rid] = targets[_rid]
                                        except InvalidResponse:
                                            pass  # rusak -> terjemahkan ulang
                                    if _valid:
                                        cache.save_batch(ch_job_id, ch_fps, _valid)
                                        meter.inherit(_valid.keys())
                                        done = len(targets)
                                        cache.state(ch_job_id, book.source_hash,
                                                    settings, "running", done, total)
                                        notify(f"Bab {ch_idx}: {len(_valid)} unit "
                                               f"diwariskan dari terjemahan sebelumnya; "
                                               f"{len(_aff_runs)} run diterjemahkan ulang.")
                        ch_pending = [u.subset([r for r in u.runs if r.id not in targets])
                                      for u in ch_units
                                      if any(r.id not in targets for r in u.runs)]
                        notify(f"Bab {ch_idx}/{n_chapters}: {len(ch_pending)} unit pending, "
                               f"{len(ch_glossary)} istilah glossary.")
                        if ch_pending:
                            ch_translator = _new_translator(ch_options)
                            try:
                                # v2.6.0: injeksi catatan karakter per bab (opt-in,
                                # anti-spoiler via notes_for_chapter); "" bila OFF
                                # sehingga prompt byte-identik dengan semula.
                                ch_notes = (mem.character_block(chapter=ch_idx)
                                            if options.character_notes else "")
                                ch_packer = TokenPacker(
                                    ch_options, TokenEstimator(cache, ch_options.model),
                                    controller, PromptBuilder(ch_options,
                                                              chapter_notes=ch_notes))
                                drain(ch_pending, ch_packer, ch_translator,
                                      make_commit(ch_job_id))
                            finally:
                                # v2.5.2: tutup pool koneksi segera setelah bab
                                # selesai — sebelumnya translator per bab menumpuk
                                # terbuka hingga seluruh buku selesai.
                                try:
                                    ch_translator.close()
                                except Exception:
                                    pass
                        # ---- PUBLICATION BARRIER ----
                        # Idempoten via discovery_log: bab yang sudah published
                        # dilewati tanpa API call. ignore_cache memaksa discovery
                        # ulang dengan menghapus penanda published bab ini.
                        if options.ignore_cache:
                            _lk = str(ch_idx)
                            if mem.discovery_log.get(_lk, {}).get("status") == "published":
                                del mem.discovery_log[_lk]
                        res = ga.run_chapter_discovery(
                            ch_idx, ch_units, mem, store, ai_client,
                            policy, options.auto_verify, cancel_event,
                            manual_glossary=options.glossary)
                        mem.translation_progress = max(mem.translation_progress, ch_idx)
                        # v2.6.0: catat namespace + snapshot glossary bab ini
                        # untuk selective re-translation di run berikutnya.
                        _ghash = glossary_fingerprint(ch_glossary)
                        mem.chapter_namespaces[str(ch_idx)] = {
                            "job_id": ch_job_id, "ghash": _ghash,
                            "config_id": config_id}
                        if _ghash not in mem.glossary_snapshots:
                            mem.glossary_snapshots[_ghash] = dict(ch_glossary)
                        store.save(mem)
                        notify(f"Bab {ch_idx} terbit: {res.discovered} istilah ditemukan "
                               f"({res.auto_approved} auto-approved, {res.pending} pending review).")

                if _v1 is None:
                    # Jalur legacy: perilaku identik seperti sebelumnya.
                    if pending:
                        builder = (getattr(translator, "builder", None)
                                   or PromptBuilder(options))
                        estimator = (getattr(translator, "estimator", None)
                                     or TokenEstimator(cache, options.model))
                        packer = TokenPacker(options, estimator, controller, builder)
                        drain(pending, packer, translator, make_commit(job_id))
                else:
                    _run_chapters_v1()
                check_cancel(cancel_event)
                if set(targets) != set(runs):
                    raise InvalidResponse("Cakupan terjemahan belum lengkap.")
                notify("Memvalidasi struktur XML, CRC ZIP, dan keutuhan aset EPUB.")
                final_performance = meter.snapshot(limiter, controller)
                validation = book.export(
                    targets, output, options.overwrite, options.epubcheck_jar, cancel_event)
                unchanged_long = [r.id for r in runs.values()
                                  if len(r.text) > 100 and targets[r.id] == r.text]
                validation.update({
                    "source_sha256": book.source_hash, "output_sha256": digest(output.read_bytes()),
                    "job_id": job_id, "engine": settings["engine"], "model": options.model,
                    "requests_this_session": limiter.requests_made,
                    "performance": final_performance,
                    "unchanged_long_runs": unchanged_long,
                    "quality_note": "Pemeriksaan struktur bukan jaminan akurasi terjemahan AI.",
                    "language_metadata": "English -> id; metadata lainnya dipertahankan",
                })
                report_path = output.with_suffix(".report.json")
                message = "Terjemahan selesai; EPUB lolos pemeriksaan integritas internal."
                try:
                    atomic_bytes(report_path, (canonical(validation) + "\n").encode("utf-8"),
                                 overwrite=options.overwrite)
                except OSError:
                    report_path = None
                    message += " Laporan terpisah gagal ditulis, tetapi EPUB sudah tersedia."
                if unchanged_long:
                    message += f" Periksa {len(unchanged_long)} potongan panjang yang tetap sama dengan sumber."
                if book.warnings:
                    message += f" Ada {len(book.warnings)} catatan dari EPUB sumber; lihat laporan."
                try:
                    cache.state(job_id, book.source_hash, settings, "complete", done, total)
                except CacheError:
                    message += " Status sesi gagal diperbarui, tetapi EPUB sudah tersedia dan hasil teks sudah di-cache."
                return TranslationResult(True, book.title, total, done, limiter.requests_made,
                                         message, output, report_path, job_id=job_id)
            except Paused as exc:
                cache.state(job_id, book.source_hash, settings, "paused", done, total)
                return TranslationResult(False, book.title, total, done, limiter.requests_made,
                                         str(exc), job_id=job_id)
            except Exception:
                cache.state(job_id, book.source_hash, settings, "failed", done, total)
                raise
            finally:
                emit_metrics()
                controller.persist()  # G2 (T-GEMINI-2): force-persist state tuning saat pause/selesai/error
                if translator is not None:
                    translator.close()
                for _tr in v1_translators:
                    try:
                        _tr.close()
                    except Exception:
                        pass
    except Paused as exc:
        return TranslationResult(False, book.title, total, done, requests, str(exc), job_id=job_id)
