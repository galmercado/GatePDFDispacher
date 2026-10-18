"""Progress of long actions. A browser starts an action with a random `job` token and polls GET /jobs/{token} to draw
the popup with the percentage ring; the server side reports with `Reporter(token)(fraction, label)`."""
import re
import threading
import time
from typing import Callable

JOBS: dict[str, dict] = {}
_lock = threading.Lock()
_KEEP_SECONDS = 900


def clean_token(token: str) -> str:
    return re.sub(r"[^\w-]", "", token or "")[:40]


class Reporter:
    """Callable `report(fraction 0..1, label=None)`. Never goes backwards; does nothing without a token."""

    def __init__(self, token: str = "", start: float = 0.0, end: float = 1.0, parent: "Reporter | None" = None):
        self.token, self.start, self.end, self.parent = clean_token(token), start, end, parent

    def __call__(self, fraction: float, label: str | None = None) -> None:
        fraction = min(1.0, max(0.0, fraction))
        if self.parent is not None:
            return self.parent(self.start + (self.end - self.start) * fraction, label)
        if not self.token:
            return
        now = time.time()
        with _lock:
            for key in [k for k, v in JOBS.items() if now - v["t"] > _KEEP_SECONDS]:
                del JOBS[key]
            job = JOBS.setdefault(self.token, {"pct": 0, "label": "", "t": now})
            job["pct"] = max(job["pct"], int(fraction * 100))
            job["t"] = now
            if label:
                job["label"] = label

    def part(self, start: float, end: float) -> "Reporter":
        """A reporter whose 0..1 maps onto [start, end] of this one."""
        return Reporter(start=start, end=end, parent=self)

    def done(self) -> None:
        if self.parent is None and self.token:
            with _lock:
                JOBS.pop(self.token, None)


def status(token: str) -> dict:
    with _lock:
        job = JOBS.get(clean_token(token))
        return {"pct": job["pct"], "label": job["label"]} if job else {"pct": 0, "label": ""}


def call(progress: Callable | None, fraction: float, label: str | None = None) -> None:
    if progress is not None:
        progress(fraction, label)
