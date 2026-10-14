"""Run output: friendly emoji lines for people, or one JSON object per line for machines."""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading


class Reporter:
    def __init__(self, fmt: str = "text", stream=None, quiet: bool = False) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event: str, text: str | None = None, level: str = "info", **fields) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._write(event=event, text=text, level=level, fields=fields)
            except BrokenPipeError:
                # Output piped into `head` or a closed log reader: keep replicating, stop printing.
                self._closed = True
                if self.stream is sys.stdout:
                    # Python flushes stdout again at exit; point it at /dev/null so that doesn't fail.
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, sys.stdout.fileno())

    def _write(self, event: str, text: str | None, level: str, fields: dict) -> None:
        if self.fmt == "json":
            record = {"ts": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="milliseconds"),
                      "event": event, "level": level, **{k: v for k, v in fields.items() if v is not None}}
            if text:
                record["message"] = text
            self.stream.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        elif text and not (self.quiet and level == "info"):
            self.stream.write(text + "\n")
        self.stream.flush()


def fmt_rows(n: int | None) -> str:
    return "?" if n is None else f"{n:,}"


def fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
