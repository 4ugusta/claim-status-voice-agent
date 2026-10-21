"""Every step of one call, in order, with its time from the start of the call.

Each step is printed as it happens, appended to logs/<call id>.jsonl, and published to the room on the
topic "claimcall.step", so scripts/call.py can show a call that runs on a remote worker. The last line
of the file is the call's summary.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("claimcall")

TOPIC = "claimcall.step"

# The steps a call can take, so a log can be read without the code.
STEPS = {
    "dialing": "placing the call",
    "answered": "the line picked up",
    "no_answer": "nobody picked up",
    "redialing": "the dial failed in a way worth one more try",
    "answered_by": "who answered: a phone menu, a person or voicemail",
    "heard": "what the helpline said",
    "said": "what the agent said",
    "pressed": "keys the agent pressed",
    "claim_number_given": "the agent gave the claim number",
    "status_captured": "the claim's status, as heard",
    "live_person": "a person came on the line",
    "stuck": "the agent stopped making progress",
    "handoff": "a person was brought onto the call",
    "hung_up": "the call ended",
    "summary": "what the call found",
}


class CallLog:
    def __init__(
        self,
        call_id: str,
        log_dir: str | Path,
        publish: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.call_id = call_id
        self.path = Path(log_dir) / f"{call_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._publish = publish
        self._clock = clock
        self._start = clock()
        self.steps: list[dict] = []

    def step(self, kind: str, **detail) -> dict:
        if kind not in STEPS:
            raise ValueError(f"unknown step {kind!r}")
        entry = {
            "t": round(self._clock() - self._start, 2),
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "step": kind,
            **detail,
        }
        self.steps.append(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("%6.2fs  %-18s %s", entry["t"], kind, describe(entry))
        if self._publish is not None:
            try:
                self._publish(json.dumps(entry))
            except Exception as exc:  # a watcher going away must never stop the call
                logger.debug("could not publish a step: %s", exc)
        return entry

    def summary(self, **extra) -> dict:
        """The call in one record: what was found and how the call got there."""
        by_kind = {}
        for s in self.steps:
            by_kind.setdefault(s["step"], []).append(s)
        status = by_kind.get("status_captured", [{}])[-1]
        answered = by_kind.get("answered_by", [{}])[-1]
        return self.step(
            "summary",
            outcome=extra.pop("outcome", "status_found" if status else "no_status"),
            claim_status=status.get("status"),
            heard_verbatim=status.get("verbatim"),
            answered_by=answered.get("category"),
            keys_pressed="".join(s["keys"] for s in by_kind.get("pressed", [])),
            live_person=bool(by_kind.get("live_person")),
            handed_off=bool(by_kind.get("handoff")),
            seconds=round(self._clock() - self._start, 1),
            **extra,
        )


def describe(entry: dict) -> str:
    """One readable line for a step, for the console."""
    d = {k: v for k, v in entry.items() if k not in ("t", "at", "step")}
    if entry["step"] in ("heard", "said"):
        return f"“{d.get('text', '')}”"
    if entry["step"] == "pressed":
        return f"{d.get('keys')}  ({d.get('why', '')})"
    return "  ".join(f"{k}={v}" for k, v in d.items())
