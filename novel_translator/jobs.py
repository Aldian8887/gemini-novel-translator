from __future__ import annotations

import threading

from .pipeline import translate_novel


def safe_error(error, secret=""):
    # API error messages are already sanitized at the transport boundary.
    message = str(error)
    secrets = secret if isinstance(secret, list) else [secret]
    for item in secrets:
        if item:
            message = message.replace(item, "[key disembunyikan]")
    return message[:1200] or type(error).__name__


class BackgroundJob:
    """Worker never calls Streamlit APIs. UI polls a bounded, locked snapshot."""
    def __init__(self, source, output, options, runner=translate_novel):
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.state = {"done": 0, "total": 0, "requests": 0, "message": "Memeriksa EPUB...",
                      "running": True, "result": None, "error": None, "metrics": {}}

        def progress(done, total, requests, message):
            with self.lock:
                self.state.update(done=done, total=total, requests=requests, message=message)

        def metrics(values):
            with self.lock:
                self.state["metrics"] = dict(values)

        progress.metrics = metrics

        def work():
            try:
                result = runner(source, output, options, progress=progress, cancel_event=self.cancel_event)
                with self.lock:
                    self.state.update(result=result, message=result.message)
            except Exception as exc:
                with self.lock:
                    self.state["error"] = safe_error(exc, options.keys())
            finally:
                options.api_key = ""
                options.api_keys = []
                with self.lock:
                    self.state["running"] = False

        self.thread = threading.Thread(target=work, daemon=True, name="epub-translator")
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def pause(self):
        self.cancel_event.set()
