"""Place a call and watch it.

    python scripts/call.py --to +15551234567 --claim 48213     # a real phone call through your SIP trunk
    python scripts/call.py --loopback --claim 48213             # no phone line: both agents in one room

It sends the job to the agent worker (and, for loopback, to the mock helpline), then joins the call as a
hidden listener: it prints each step the agent publishes, records both sides to logs/<call>.wav (agent on
the left, helpline on the right), and exits with the call's summary. The workers must be running:
    python -m claimcall.caller dev
    python -m claimcall.helpline dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from livekit import api, rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claimcall import config  # noqa: E402
from claimcall.calllog import STEPS, TOPIC, describe  # noqa: E402

RATE = 24000
COLOURS = {
    "pressed": "\033[33m",
    "status_captured": "\033[32m",
    "live_person": "\033[36m",
    "handoff": "\033[35m",
    "stuck": "\033[31m",
    "no_answer": "\033[31m",
    "summary": "\033[1m",
}


class Recorder:
    """Both sides of the call on a shared clock, so silences stay where they were."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.started_at = datetime.now(UTC)  # the steps' wall-clock times are placed against this
        self.tracks: dict[str, list[np.ndarray]] = {"agent": [], "helpline": []}
        self.cursor: dict[str, int] = {"agent": 0, "helpline": 0}

    def add(self, side: str, frame: rtc.AudioFrame) -> None:
        """Each side arrives as a continuous stream, silence included (measured: exactly real time, both sides), so
        it is placed once by its first frame and then written as it comes. Placing every frame by its arrival
        time put a burst that came in late after the reply to it."""
        samples = np.frombuffer(frame.data, dtype=np.int16)
        if self.cursor[side] == 0:
            lead = max(int((time.monotonic() - self.t0) * RATE) - len(samples), 0)
            self.tracks[side].append(np.zeros(lead, dtype=np.int16))
            self.cursor[side] = lead
        self.tracks[side].append(samples)
        self.cursor[side] += len(samples)

    def save(self, path: Path) -> float:
        left, right = (
            np.concatenate(self.tracks[s]) if self.tracks[s] else np.zeros(1, np.int16) for s in ("agent", "helpline")
        )
        n = max(len(left), len(right))
        stereo = np.zeros((n, 2), dtype=np.int16)
        stereo[: len(left), 0], stereo[: len(right), 1] = left, right
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
            w.writeframes(stereo.tobytes())
        return n / RATE


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", help="the helpline's number, E.164 (+15551234567)")
    ap.add_argument("--claim", required=True, help="the claim number to ask about")
    ap.add_argument("--loopback", action="store_true", help="no phone line: the mock helpline joins the room")
    ap.add_argument(
        "--variant",
        choices=["standard", "representative"],
        default="standard",
        help="loopback only: 'representative' removes the claims option, so a person must answer",
    )
    args = ap.parse_args()
    if not args.loopback and not args.to:
        ap.error("give --to, or --loopback for a test with no phone line")

    room = f"claim-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    lk = api.LiveKitAPI()
    meta = {"claim_number": args.claim, "phone_number": args.to or "", "loopback": args.loopback}
    if args.loopback:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=config.HELPLINE_AGENT,
                room=room,
                metadata=json.dumps({"loopback": True, "variant": args.variant}),
            )
        )
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name=config.CALLER_AGENT, room=room, metadata=json.dumps(meta))
    )

    token = (
        api.AccessToken()
        .with_identity("observer")
        .with_name("observer")
        .with_grants(api.VideoGrants(room_join=True, room=room, hidden=True, can_publish=False))
        .to_jwt()
    )
    lobby = rtc.Room()
    rec = Recorder()
    done = asyncio.Event()
    summary: dict = {}
    tasks: list[asyncio.Task] = []

    def on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != TOPIC:
            return
        entry = json.loads(packet.data)
        colour = COLOURS.get(entry["step"], "")
        print(f"{entry['t']:7.2f}s  {colour}{entry['step']:<18}\033[0m {describe(entry)}", flush=True)
        if entry["step"] == "summary":
            summary.update(entry)
            done.set()

    def on_track(track: rtc.Track, _pub, participant: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        side = (
            "helpline"
            if (
                participant.identity == config.HELPLINE_IDENTITY
                or participant.attributes.get("claimcall.role") == "helpline"
            )
            else "agent"
        )

        async def pump() -> None:
            stream = rtc.AudioStream(track, sample_rate=RATE, num_channels=1)
            try:
                async for ev in stream:
                    rec.add(side, ev.frame)
            finally:
                await stream.aclose()

        tasks.append(asyncio.create_task(pump()))

    lobby.on("data_received", on_data)
    lobby.on("track_subscribed", on_track)
    lobby.on("disconnected", lambda *_: done.set())
    await lobby.connect(config_url(), token)
    print(f"call {room}: claim {args.claim}, {'loopback' if args.loopback else 'to ' + args.to}")
    print("steps: " + ", ".join(STEPS))
    try:
        await asyncio.wait_for(done.wait(), timeout=config.MAX_CALL_SECONDS + 60)
    except TimeoutError:
        print("no summary in time")
    await asyncio.sleep(1.0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await lobby.disconnect()
    await lk.aclose()
    out = Path(config.LOG_DIR) / f"{room}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds = rec.save(out)
    mode = (
        "loopback: one LiveKit room, no phone line" if args.loopback else f"phone call to {args.to[:2]}…{args.to[-4:]}"
    )
    out.with_suffix(".meta.json").write_text(
        json.dumps(
            {"recording_started_at": rec.started_at.isoformat(), "claim": args.claim, "mode": mode, "room": room}
        )
    )
    print(f"\nrecording: {out} ({seconds:.0f} s, agent left, helpline right)")
    print(f"log: {Path(config.LOG_DIR) / (room + '.jsonl')} (written by the agent worker)")
    return 0 if summary.get("outcome") == "status_found" else 1


def config_url() -> str:
    return os.environ["LIVEKIT_URL"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
