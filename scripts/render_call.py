"""Turn a finished call into a video: the call's own stereo recording, with each logged step shown at the
moment it happened and a level meter for each side.

    python scripts/render_call.py logs/<call>.jsonl            # writes logs/<call>.mp4

It needs the call's .jsonl (written by the agent), its .wav and .meta.json (written by scripts/call.py), and
ffmpeg. The steps are placed by their wall-clock time against the recording's start, so the picture and the
sound are the same call, not a reconstruction.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 12
BG, PANEL, LINE = (14, 17, 22), (22, 26, 33), (38, 44, 54)
TEXT, DIM, FAINT = (226, 232, 240), (148, 163, 184), (100, 116, 139)
COLOURS = {
    "pressed": (250, 204, 21),
    "claim_number_given": (250, 204, 21),
    "status_captured": (74, 222, 128),
    "live_person": (56, 189, 248),
    "handoff": (232, 121, 249),
    "stuck": (248, 113, 113),
    "answered_by": (165, 180, 252),
    "hung_up": (148, 163, 184),
    "summary": (255, 255, 255),
    "heard": (203, 213, 225),
    "said": (125, 211, 252),
}
AGENT, HELPLINE = (125, 211, 252), (203, 213, 225)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size, index=1 if bold else 0)


def detail(e: dict) -> str:
    d = {k: v for k, v in e.items() if k not in ("t", "at", "step", "v")}
    if e["step"] in ("heard", "said"):
        return f"“{d.get('text', '')}”"
    if e["step"] == "pressed":
        return f"{d.get('keys')}  ({d.get('why', '')})"
    if e["step"] == "answered_by":
        return d.get("category", "")
    if e["step"] == "status_captured":
        return f"{d.get('status')}   “{d.get('verbatim', '')}”"
    if e["step"] == "summary":
        return (
            f"outcome={d.get('outcome')}  status={d.get('claim_status')}  "
            f"keys={d.get('keys_pressed')}  {d.get('seconds')} s"
        )
    return "  ".join(f"{k}={v}" for k, v in d.items() if k not in ("greeting", "evidence"))[:110]


def main() -> None:
    log = Path(sys.argv[1])
    stem = log.with_suffix("")
    steps = [json.loads(line) for line in log.read_text().splitlines()]
    meta = json.loads(stem.with_suffix(".meta.json").read_text())
    start = datetime.fromisoformat(meta["recording_started_at"])
    for e in steps:
        e["v"] = (datetime.fromisoformat(e["at"]) - start).total_seconds()

    with wave.open(str(stem.with_suffix(".wav"))) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2).astype(np.float32)
    duration = len(audio) / rate
    summary = next((e for e in steps if e["step"] == "summary"), {})
    seconds = min(duration, max((e["v"] for e in steps), default=duration) + 2.0)

    out = stem.with_suffix(".mp4")
    ff = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{W}x{H}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-i",
            str(stem.with_suffix(".wav")),
            "-t",
            f"{seconds:.2f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out),
        ],
        stdin=subprocess.PIPE,
    )
    f_title, f_head, f_body, f_small = font(26, True), font(17, True), font(16), font(14)
    claim = summary.get("claim_number") or meta.get("claim", "")
    for i in range(int(seconds * FPS)):
        now = i / FPS
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((40, 28), "claim-status-voice-agent", font=f_title, fill=TEXT)
        d.text((40, 66), f"claim {claim}  ·  {meta.get('mode', '')}  ·  {stem.name}", font=f_small, fill=DIM)
        d.text((W - 150, 34), f"{now:5.1f} s", font=f_title, fill=DIM)

        # the two sides, as level meters
        for row, (label, ch, colour) in enumerate((("agent", 0, AGENT), ("helpline", 1, HELPLINE))):
            seg = audio[int(now * rate) : int((now + 1 / FPS) * rate), ch]
            level = min(1.0, float(np.sqrt(np.mean(seg**2))) / 6000) if len(seg) else 0.0
            y = 112 + row * 34
            d.text((40, y), label, font=f_head, fill=colour if level > 0.05 else FAINT)
            d.rounded_rectangle((150, y + 4, W - 40, y + 20), radius=6, fill=PANEL)
            if level > 0.01:
                d.rounded_rectangle((150, y + 4, 150 + int((W - 190) * level), y + 20), radius=6, fill=colour)

        # the steps so far, newest at the bottom
        d.line((40, 190, W - 40, 190), fill=LINE)
        shown = [e for e in steps if e["v"] <= now]
        lines = []
        for e in shown:
            wrapped = textwrap.wrap(detail(e), 84) or [""]
            lines.append((e, wrapped[0], True))
            lines += [(e, extra, False) for extra in wrapped[1:2]]
        rows = (H - 40 - 205) // 26  # below the divider, newest at the bottom
        y = H - 40 - 26 * min(len(lines), rows)
        for e, text, first in lines[-rows:]:
            colour = COLOURS.get(e["step"], DIM)
            if first:
                d.text((40, y), f"{e['t']:6.2f}s", font=f_small, fill=FAINT)
                d.text(
                    (130, y - 1),
                    e["step"],
                    font=f_head if e["step"] in ("status_captured", "summary") else f_body,
                    fill=colour,
                )
            d.text((340, y), text, font=f_body, fill=colour if e["step"] != "heard" else TEXT)
            y += 26
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    ff.wait()
    print(f"{out} ({seconds:.0f} s)")


if __name__ == "__main__":
    main()
