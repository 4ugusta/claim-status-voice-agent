"""The outbound agent: calls an insurer's helpline, works through its phone menu, gives the claim number,
asks for the claim's status, and logs every step of the call (claimcall.calllog).

How a call goes:
1. The session starts and answering machine detection (LiveKit's AMD) listens before anything is dialled,
   so the first words of the line are never missed.
2. The agent dials out through a SIP trunk and waits until the line picks up. In a loopback test there is
   no phone line: the mock helpline is already in the room.
3. AMD classifies the greeting: a phone menu, a person, or voicemail. Voicemail ends the call: there is no
   one to ask.
4. A language model listens to the line and acts only through tools: press keys, give the claim number,
   record the status, note that a person came on, ask for a person when stuck, hang up. While a menu is
   talking the agent is muted; a recording cannot hear it, and talking over it only earns a "sorry, I did
   not understand".
5. A status is only recorded if the line actually said it: the words are checked against what was heard.
6. Stuck (too many prompts without progress, or the call running too long): a person is dialled into the
   call and briefed, and the agent leaves them to it. With no number set, the call ends and says so.

Run: python -m claimcall.caller dev
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from array import array
from dataclasses import dataclass

from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    StopResponse,
    WorkerOptions,
    cli,
    function_tool,
    room_io,
)
from livekit.agents.voice.amd import AMD

from claimcall import claims, config
from claimcall.calllog import TOPIC, CallLog

logger = logging.getLogger("claimcall.caller")

DTMF_CODES = {**{str(d): d for d in range(10)}, "*": 10, "#": 11}
KEY_GAP_SECONDS = 0.3  # between keys, so a phone system registers each one
# The two frequencies a phone plays for each key (ITU-T Q.23).
DTMF_HZ = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}  # fmt: skip
TONE_SECONDS = 0.15
# A dial refused with a login error or timed out is tried once more: on 22 Oct three SIP calls in about thirty
# were refused (401) and each went through when dialled again. Busy or no such number is final.
REDIAL_SECONDS = 2.0


def worth_redialing(error: str) -> bool:
    return bool(re.search(r"\b401\b|timed out|timeout", error or "", re.I))


def keypad_tones(keys: str, rate: int) -> list[rtc.AudioFrame]:
    """The keys as a phone plays them: each key's two tones, then quiet until the next key, in 20 ms frames."""
    samples = array("h")
    per_key, tone = int(rate * KEY_GAP_SECONDS), int(rate * TONE_SECONDS)
    for k in keys:
        low, high = DTMF_HZ[k]
        samples.extend(
            int(6000 * (math.sin(2 * math.pi * low * i / rate) + math.sin(2 * math.pi * high * i / rate)))
            for i in range(tone)
        )
        samples.extend([0] * (per_key - tone))
    step = rate // 50
    return [
        rtc.AudioFrame(samples[i : i + step].tobytes(), rate, 1, len(samples[i : i + step]))
        for i in range(0, len(samples), step)
    ]


# Speech to text runs about a second behind the audio, so words heard just after a key press are still the
# prompt that was playing before it (19 Oct: "for pharmacy benefits" arrived after pressing 1, and the agent
# pressed 3). Only speech heard this long after a key counts as the line answering.
SETTLE_SECONDS = 2.0

INSTRUCTIONS = """You are an automated assistant calling an insurance company's phone line for a doctor's \
billing office. Your one job is to find out the status of claim number {claim} and record it.

How to do it:
- A phone menu is a recording. It cannot hear you unless it asks you to say something. Choose options \
with press_keys: pick the option for claims or claim status. Billing and pharmacy are not claims: if no \
option says claims, press the one for a representative or an operator. If you have not yet heard an option \
for claims, let the menu finish before choosing: the option you need may come last. Press an option once, then \
wait for the next prompt. Never press menu options while the line is asking for the claim number.
- When the line asks for the claim number, call give_claim_number: use "keypad" when it says enter or \
press, and "voice" when it says say.
- When the line states the claim's status, call record_claim_status with the status and the sentence you heard.
- If a person speaks (they greet you, give a name, or ask how they can help), call note_live_person once: \
it introduces you and gives them the claim number. Then wait for their answer. Answer their questions in \
one short sentence, never repeat yourself, and never say the claim number in your own words (give_claim_number \
reads it). If they say they are checking or ask you to hold, say nothing at all.
- Never guess or invent a status, and never give any information other than the claim number.
- If the menu has no option that gets you closer, repeats itself, cannot find the claim, or you do not know \
what to do, call ask_for_human.
- When the status is recorded, thank a person in one short sentence, then call end_call.
Reply with tool calls only while a menu is speaking. Keep anything you say to a person to one short sentence, \
and after asking them something, wait for their answer: never repeat a question while they look it up."""


@dataclass
class CallJob:
    claim_number: str
    phone_number: str = ""
    loopback: bool = False

    @classmethod
    def from_metadata(cls, raw: str) -> CallJob:
        data = json.loads(raw or "{}")
        claim = claims.digits_in(str(data.get("claim_number", "")))
        if not claim:
            raise ValueError("the job needs a claim_number")
        loopback = bool(data.get("loopback"))
        phone = str(data.get("phone_number", "")).strip()
        if not loopback and not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            raise ValueError("the job needs phone_number in E.164 form, for example +15551234567")
        return cls(claim_number=claim, phone_number=phone, loopback=loopback)


def masked(number: str) -> str:
    return number[:2] + "•" * max(len(number) - 6, 0) + number[-4:] if number else ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def stated_on_line(status: str, heard: list[str]) -> bool:
    """The status's words, in order, appear in what the line said: the model may only write down what it heard."""
    want, got = _words(status), " ".join(_words(" ".join(heard)))
    return bool(want) and " ".join(want) in got


MENU_CUES = re.compile(
    r"\bpress\b|\bpound\b|\bkeypad\b|status of this claim|claim number|main menu|thank you for calling", re.I
)
PERSON_CUES = re.compile(
    r"\bthis is\b|\bmy name\b|\bspeaking\b|how (can|may) i help|\bhello\b|\bhi\b|"
    r"\bthanks for holding\b|\bwho am i\b|can i (get|have)",
    re.I,
)
FAILURE_CUES = re.compile(
    r"could not|couldn't|cannot|can't|not find|not valid|invalid|sorry|unable|"
    r"not available|try again|closed|did not understand|didn't understand",
    re.I,
)


def sounds_like_person(text: str) -> bool:
    """A person greets, gives a name or offers help; a menu reads options. Menu words rule a person out."""
    return bool(PERSON_CUES.search(text)) and not MENU_CUES.search(text)


USEFUL_OPTION = re.compile(
    r"claim|status|representative|operator|agent|person|human|someone|repeat|again|"
    r"main menu|\bback\b|previous",
    re.I,
)
_DIGIT_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "star": "*",
    "pound": "#",
}
_OPTION = re.compile(r"(?:for|to)\s+([a-z' ]+?),?\s+press\s+(\w+)|press\s+(\w+)\s+(?:for|to)\s+([a-z' ]+)", re.I)


def menu_options(heard: list[str]) -> dict[str, str]:
    """The options a menu read out, key to label: "For billing, press two" gives {"2": "billing"}."""
    text = " ".join(heard).lower().replace(".", ",")
    out: dict[str, str] = {}
    for m in _OPTION.finditer(text):
        label, key = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        key = _DIGIT_WORDS.get(key, key)
        if key in DTMF_CODES:
            out[key] = label.strip()
    return out


QUESTION = re.compile(r"\?|^(can|could|may|would|will|what|which|who|when|where|how|is|are|do|does)\b", re.I)


def asks_something(line: str) -> bool:
    return bool(QUESTION.search(line.strip()))


def speakable(text: str) -> str:
    """What the model wrote, fit to say: no stage directions in brackets, and numbers read digit by digit, since
    "48213" is spoken as "forty eight thousand two hundred thirteen" and was heard that way."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", text)
    text = re.sub(r"\d{4,}", lambda m: ", ".join(m.group()), text)
    return " ".join(text.split())


ASK_CUES = re.compile(r"\bnumber\b|\benter\b|\bwhich claim\b|\bwhat claim\b|\breference\b", re.I)


def asks_for_number(heard: list[str]) -> bool:
    return any(ASK_CUES.search(line) for line in heard)


def line_reports_failure(heard: list[str]) -> bool:
    return any(FAILURE_CUES.search(line) for line in heard)


# What a person brought onto the call is told, by code. Nothing from the call itself is ever read to them.
HANDOFF_REASONS = {
    "repeating": "the menu keeps repeating itself without an option for claims",
    "no_progress": "the call stopped making progress",
    "too_long": "the call has run too long without a status",
    "line_failed": "their line says it cannot find or handle the claim",
    "person_cannot_help": "the person on their side could not give me the status",
    "no_status": "I could not get the status",
}


def stuck_reason(since_progress: list[str], seconds: float) -> str | None:
    """Why the call is stuck, or None. Stuck is the line repeating itself or time passing, never fragments."""
    counts: dict[str, int] = {}
    for line in since_progress:
        key = " ".join(_words(line))
        if len(key.split()) >= 3:
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= config.REPEATS_BEFORE_STUCK:
                return f"the line said “{line.strip()}” {counts[key]} times without my option"
    if seconds >= config.NO_PROGRESS_SECONDS:
        return f"{int(seconds)} seconds without getting closer to the status"
    return None


class ClaimCaller(Agent):
    def __init__(self, job: CallJob, log: CallLog, ctx: JobContext) -> None:
        super().__init__(instructions=INSTRUCTIONS.format(claim=claims.spoken(job.claim_number)))
        self.job, self.log, self.ctx = job, log, ctx
        self.person = False  # a person is on the line, so the agent may speak
        self.status: str | None = None
        self.heard_lines: list[str] = []
        self.active = False  # AMD has spoken: before that the agent may not act, so nothing counts
        self.last_progress = time.monotonic()
        self.since_progress: list[str] = []
        self.since_claim: list[str] = []
        self.since_key: list[str] = []  # what the line said since the last key: a menu key needs a menu
        self.last_activity = time.monotonic()  # anyone speaking; silence is watched (watch_silence)
        self.nudges = 0
        self.last_nudge = 0.0
        self.watching = False
        # Lines heard before the agent's last scripted line are already answered by it: "how can I help you"
        # arrives while the opener plays. The model may answer only questions asked after it.
        self.answered_upto = 0
        self.finished = False
        self.handing_off = False
        # After a key or the claim number, the agent waits for the line to answer before doing anything else:
        # a model can chain tool calls in one turn, and once called "stuck" two seconds before the status came.
        self.waiting_for_line = False
        self.acted_at = 0.0
        # The claim number is given once, unless the line says it could not find it or asks again.
        self.claim_given = False
        self.asked_again = False
        # The opener carries the number. While it waits for the person to pause, nothing else may say it: the
        # model once noted the person and gave the number in one turn, and the number was said twice in a row.
        self.opener_pending = False

    # --- what the line says ---------------------------------------------------------------------------------

    def heard(self, text: str) -> None:
        self.last_activity = time.monotonic()
        self.heard_lines.append(text)
        self.since_progress.append(text)
        self.since_key.append(text)
        if self.claim_given:
            self.since_claim.append(text)
        if time.monotonic() - self.acted_at >= SETTLE_SECONDS:
            self.waiting_for_line = False
        if self.claim_given and re.search(
            r"could not|cannot|can't|not find|not valid|invalid|again|re-?enter", text, re.I
        ):
            self.asked_again = True
        if self.person and self.claim_given and ASK_CUES.search(text):
            self.asked_again = True  # a person asking for the number gets it again
        self.log.step("heard", text=text)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if self.finished:
            raise StopResponse()
        if self.active:
            reason = stuck_reason(self.since_progress, time.monotonic() - self.last_progress)
            if reason:
                await self.hand_off("repeating" if "times without" in reason else "no_progress", reason)
                raise StopResponse()

    def _progress(self) -> None:
        self.last_progress = time.monotonic()
        self.since_progress = []

    # --- what the agent says: nothing to a recording ------------------------------------------------------------

    async def tts_node(self, text, model_settings):
        said = "".join([chunk async for chunk in text]).strip()
        spoken = speakable(said)
        if not self.person or self.opener_pending or not spoken or not self._may_speak():
            # Muted while a menu talks, and with a person unless they asked something: left free, the model
            # repeated "I need the status of claim 48213" four times while the person looked it up.
            return

        async def once():
            yield spoken

        async for frame in Agent.default.tts_node(self, once(), model_settings):
            yield frame
        self.log.step("said", text=spoken)

    def _may_speak(self) -> bool:
        if self.status is not None:
            return True  # the status is in: thank them
        # Only a question asked after the agent's last scripted line. A timer here once held back the agent's
        # "yes" to "is that four eight two one three?" and both sides went quiet until the time limit.
        return any(asks_something(line) for line in self.heard_lines[self.answered_upto :])

    def say(self, text: str, *, interruptible: bool | None = None) -> None:
        """A scripted line, word for word. It is synthesised here and handed over as audio, so it never passes the
        gate on the model's own words (tts_node): through that gate, the opener and the nudges were silently dropped
        while the log said they were spoken. A menu cannot be talked over, so the claim number is never cut off;
        a person can cut in, except into the opener, which carries the number."""
        self.session.say(
            text, audio=self._synth(text), allow_interruptions=self.person if interruptible is None else interruptible
        )
        self.last_activity = time.monotonic()
        self.answered_upto = len(self.heard_lines)
        self.log.step("said", text=text)

    async def _synth(self, text: str):
        stream = self.session.tts.synthesize(text)
        try:
            async for ev in stream:
                yield ev.frame
        finally:
            await stream.aclose()

    async def greet_person(self, detected_by: str, evidence: str) -> None:
        """A person is on the line: the opener and the claim number are said by code, once. Left to the model they
        were said two or three times, and it talked over the person while they gave the status."""
        if self.person:
            return
        self.person, self.opener_pending = True, True
        self.acted_at = time.monotonic()  # the model stays quiet from here until the opener is out
        self.log.step("live_person", evidence=evidence, detected_by=detected_by)
        number = self.job.claim_number
        try:
            # Wait for the person to pause: said into "how can I help you today", the opener was cut off, the
            # person never heard the number, and both sides waited until the time limit.
            await self.wait_for_quiet()
            self.say(
                "Hi, this is an automated assistant calling for a doctor's billing office. "
                f"I'm checking on the status of claim number {claims.spoken(number)}.",
                interruptible=False,
            )
        finally:
            self.opener_pending = False
        self.claim_given, self.asked_again = True, False
        self.log.step("claim_number_given", claim_number=number, how="voice")
        self.acted_at = time.monotonic()
        self._progress()

    # --- tools ----------------------------------------------------------------------------------------------

    @function_tool
    async def press_keys(self, context: RunContext, keys: str, why: str) -> str:
        """Press keys on the phone keypad, to choose a menu option.

        Args:
            keys: the keys in order, from 0-9, * and #, for example "1"
            why: the option you are choosing, in a few words, for example "claims"
        """
        if self.finished:
            return "The call is over. Do nothing more."
        if self.waiting_for_line:
            return "Wait: the line has not answered the last keys yet."
        keys = "".join(k for k in keys if k in DTMF_CODES)
        if not keys:
            return "No valid keys: use 0-9, * or #."
        if sum(k.isdigit() for k in keys) >= 3:
            return "That is a number, not a menu option: use give_claim_number when the line asks for it."
        offered = menu_options(self.since_key)
        if len(keys) == 1:
            # Judged on what the menu said since the last key, not on the reason the model gives: once it pressed
            # billing and called it "claims or claim status", and once pressed 0 again during "please hold".
            if not offered:
                return "The line has not read any options since your last key. Wait for it."
            label = offered.get(keys)
            if label is None:
                return f"The menu did not offer {keys}. It offered: " + "; ".join(
                    f"{k} {v}" for k, v in offered.items()
                )
            if not USEFUL_OPTION.search(label):
                return (
                    f"{keys} is {label}, which will not get the claim status. Choose claims, or a representative "
                    "if there is no claims option; wait for the menu to finish if you have not heard them yet."
                )
        elif not USEFUL_OPTION.search(why):
            # Billing or pharmacy is never it: a menu read only "for billing, press two" so far, and the agent
            # pressed it before the representative option came.
            return (
                "Only choose claims or claim status, a representative or operator, or repeat or back. Wait for "
                "the menu to finish if you have not heard one of those yet."
            )
        await self.send_keys(keys)
        # The menu's own words for the option, not the model's: it once logged 0, the representative, as "claims".
        self.log.step("pressed", keys=keys, why=offered.get(keys, why) if len(keys) == 1 else why)
        self._progress()
        self.waiting_for_line = True
        return f"Pressed {keys}. Now wait for the line to respond; do nothing else this turn."

    @function_tool
    async def give_claim_number(self, context: RunContext, how: str) -> str:
        """Give the claim number, when the line asks for it.

        Args:
            how: "keypad" when asked to enter or press it, "voice" when asked to say it
        """
        if self.finished:
            return "The call is over. Do nothing more."
        if self.waiting_for_line:
            return "Wait: the line has not answered yet."
        if self.opener_pending:
            return "Your opener is about to give the claim number. Wait for the person to answer it."
        if self.claim_given and not self.asked_again:
            return "You already gave the claim number. Wait for the line to answer."
        if not self.person and not asks_for_number(self.since_progress[-4:]):
            # Keyed into a menu, a claim number is just keys: "51007#" once reached the representative by its 0.
            return "Nobody has asked for the claim number yet. Wait until the line or the person asks for it."
        self.claim_given, self.asked_again = True, False
        number = self.job.claim_number
        if how == "voice" or self.person:
            self.say(f"The claim number is {claims.spoken(number)}.")
            self.acted_at = time.monotonic()
        else:
            await self.send_keys(number + "#")
            self.log.step("pressed", keys=number + "#", why="claim number")
        self.log.step(
            "claim_number_given", claim_number=number, how="voice" if how == "voice" or self.person else "keypad"
        )
        self._progress()
        self.waiting_for_line = True
        return "Given. Now wait for the line to respond; do nothing else this turn."

    @function_tool
    async def record_claim_status(self, context: RunContext, status: str, heard: str) -> str:
        """Record the claim's status once the line has stated it.

        Args:
            status: the status in a few words, as stated, for example "approved" or "pending review"
            heard: the sentence you heard that states it, word for word
        """
        if self.finished:
            return "The call is over. Do nothing more."
        if not stated_on_line(status, self.since_claim or self.heard_lines):
            # The model may only write down what the line said, never a guess.
            return "That status was not said on the line. Only record a status the line states."
        self.status = status
        self.log.step("status_captured", status=status, verbatim=heard)
        self._progress()
        if self.person:
            return "Recorded. Thank them in one short sentence, then call end_call."
        await self.hang_up("status captured")
        return "Recorded. The call is over."

    @function_tool
    async def note_live_person(self, context: RunContext, evidence: str) -> str:
        """Call once when a person, not a recording, is speaking on the line.

        Args:
            evidence: what they said that shows a person is speaking
        """
        if self.finished:
            return "The call is over. Do nothing more."
        recent = " ".join(self.since_progress[-3:]) or evidence
        if not self.person and not sounds_like_person(recent):
            return "That is the phone menu, not a person: it reads options. Keep using the keypad."
        await self.greet_person("conversation", recent)
        return (
            "You have introduced yourself and given the claim number. Wait silently for their answer. If they ask "
            "for the number again, call give_claim_number."
        )

    @function_tool
    async def ask_for_human(self, context: RunContext, reason: str) -> str:
        """Bring a person onto the call when you are stuck.

        Args:
            reason: why you are stuck, in a few words
        """
        if self.finished:
            return "The call is over. Do nothing more."
        if self.status is not None:
            # A model once asked for a human "to complete the call politely" after the status was in, and the
            # log said the person could not give the status.
            return "The status is recorded, so nobody is needed. Thank them in one sentence, then call end_call."
        if self.waiting_for_line:
            return "Not yet: the line has not answered what you just did. Wait for it."
        if not (self.person or line_reports_failure(self.since_progress[-4:])):
            # Only the line saying something failed, or a person who cannot help, justifies giving up early;
            # a line that loops is caught in code (stuck_reason). Once a model gave up mid readback.
            return "Nothing on the line says you are stuck. Keep listening and act on its next prompt."
        why = "line_failed" if line_reports_failure(self.since_progress[-4:]) else "person_cannot_help"
        await self.hand_off(why, reason)
        return "Handed off. Say nothing more."

    @function_tool
    async def end_call(self, context: RunContext, reason: str) -> str:
        """Hang up, once the status is recorded (and a person has been thanked).

        Args:
            reason: why the call is ending, in a few words
        """
        if self.status is None:
            return (
                "No status is recorded yet. If the line stated it, call record_claim_status first; if you "
                "cannot get it, call ask_for_human."
            )
        await context.wait_for_playout()
        await self.hang_up(reason)
        return "Call ended."

    async def wait_for_quiet(self, pause: float = 0.7, limit: float = 4.0) -> None:
        """Until the line has been quiet for `pause` seconds, or `limit` passes."""
        deadline, quiet_since = time.monotonic() + limit, None
        while time.monotonic() < deadline:
            if self.session.user_state == "speaking":
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= pause:
                    return
            await asyncio.sleep(0.1)

    async def watch_silence(self) -> None:
        """Stuck can be silence too, which no turn ever reports: with a person, ask once more after 12 quiet
        seconds; with anyone, hand off after NO_PROGRESS_SECONDS without progress."""
        if self.watching:
            return
        self.watching = True
        while not self.finished:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            quiet = self.session.user_state != "speaking" and self.session.agent_state != "speaking"
            if not quiet:
                # Quiet counts from the end of speech. Counted from when the eight second opener was queued, the
                # nudge came six seconds after it ended, over the person reading out the status.
                self.last_activity = now
            if (
                self.person
                and self.status is None
                and quiet
                and self.nudges < 2
                and now - max(self.last_activity, self.last_nudge) > 12
            ):
                self.nudges += 1
                self.last_nudge = now
                self.say(f"Sorry, are you able to check the status of claim {claims.spoken(self.job.claim_number)}?")
            if self.status is None and now - self.last_progress >= config.NO_PROGRESS_SECONDS:
                await self.hand_off("no_progress", f"{int(now - self.last_progress)} s without progress")

    # --- the line ---------------------------------------------------------------------------------------------

    async def send_keys(self, keys: str) -> None:
        participant = self.ctx.room.local_participant
        # The line acts on the DTMF events. The tones are played too, as a phone plays them, so a person listening
        # (or the recording) hears each press: without them the agent's side of a menu call was silent.
        frames = keypad_tones(keys, getattr(self.session.tts, "sample_rate", 24000))

        async def tones():
            for frame in frames:
                yield frame

        self.session.say(keys, audio=tones(), allow_interruptions=False, add_to_chat_ctx=False)
        for k in keys:
            await participant.publish_dtmf(code=DTMF_CODES[k], digit=k)
            await asyncio.sleep(KEY_GAP_SECONDS)
        self.acted_at = time.monotonic()
        self.since_key = []

    async def hang_up(self, reason: str) -> None:
        if self.finished:
            return
        self.finished = True
        self.log.step("hung_up", reason=reason)
        await asyncio.sleep(0.5)
        await self.ctx.api.room.delete_room(api.DeleteRoomRequest(room=self.ctx.room.name))

    async def hand_off(self, why: str, detail: str = "") -> None:
        """Task bonus: when stuck, dial a person into the call, brief them, and leave them with the line.

        `why` is one of HANDOFF_REASONS, chosen by code; it is the only thing said to the person, with the claim
        number. `detail` (the model's words, or the line's) goes to the log only, capped: either can be steered by
        whoever is on the insurer's end, and must never be read to a colleague (security review, 22 Oct 2026).
        The flag is set before the first await, so the stuck check, the watchdog, the time limit and the tool
        firing together dial the handoff number once."""
        if self.finished or self.handing_off:
            return
        if self.status is not None:
            # A call with its status needs nobody, but a limit that fires must still end it (security review,
            # 22 Oct 2026: returning here alone let the time limit pass and the call run on).
            await self.hang_up(f"status recorded ({why})")
            return
        self.handing_off = True
        brief = HANDOFF_REASONS.get(why, HANDOFF_REASONS["no_status"])
        self.log.step("stuck", reason=brief, detail=detail[:200], detail_is="untrusted text from the call")
        if self.job.loopback or not (config.HANDOFF_NUMBER and config.OUTBOUND_TRUNK_ID):
            self.log.step("handoff", result="no handoff number set: a person needs to call back", reason=brief)
            await self.hang_up("needs a person")
            return
        self.log.step("handoff", result="dialing", to=masked(config.HANDOFF_NUMBER))
        try:
            await self.ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    sip_trunk_id=config.OUTBOUND_TRUNK_ID,
                    sip_call_to=config.HANDOFF_NUMBER,
                    participant_identity="handoff-person",
                    participant_name="Billing office",
                    wait_until_answered=True,
                )
            )
        except api.TwirpError as exc:
            self.log.step("handoff", result="not answered", sip_status=exc.metadata.get("sip_status_code"))
            await self.hang_up("handoff not answered")
            return
        self.person = True
        self.say(
            f"Hi, this is the claim status assistant. I'm on the line with the insurer about claim "
            f"{claims.spoken(self.job.claim_number)}, and {brief}. You're connected to them now."
        )
        self.log.step("handoff", result="connected")
        self.finished = True  # the person has the call; the agent stops acting on it

    # --- after answer ---------------------------------------------------------------------------------------

    async def answered_by(self, category: str, transcript: str) -> None:
        self.log.step("answered_by", category=category, greeting=transcript)
        self.active = True
        self._progress()
        asyncio.create_task(self.watch_silence())
        if category == "human":
            await self.greet_person("answering machine detection", transcript)
        elif category in ("machine-vm", "machine-unavailable"):
            await self.hang_up("voicemail: no one to ask")
        else:
            # A menu (or unsure): replies were held while AMD listened, so act on what has been heard.
            self.session.generate_reply()


async def find_helpline(ctx: JobContext) -> str:
    """In a loopback room the line is the mock helpline's agent, which marks itself."""
    for _ in range(100):
        for p in ctx.room.remote_participants.values():
            if p.attributes.get("claimcall.role") == "helpline":
                return p.identity
        await asyncio.sleep(0.2)
    raise RuntimeError("the mock helpline never joined the room")


async def entrypoint(ctx: JobContext) -> None:
    job = CallJob.from_metadata(ctx.job.metadata)
    await ctx.connect()

    def publish(line: str) -> None:
        asyncio.ensure_future(ctx.room.local_participant.publish_data(line, topic=TOPIC, reliable=True))

    log = CallLog(ctx.room.name, config.LOG_DIR, publish=publish)
    agent = ClaimCaller(job, log, ctx)
    session = AgentSession(
        stt=config.STT,
        llm=config.LLM,
        tts=config.CALLER_TTS,
        max_tool_steps=4,
        turn_handling={"endpointing": {"min_delay": 0.4, "max_delay": 2.0}},
    )
    session.on(
        "user_input_transcribed", lambda ev: ev.is_final and ev.transcript.strip() and agent.heard(ev.transcript)
    )

    async def write_summary() -> None:
        log.summary(claim_number=job.claim_number, to=masked(job.phone_number) or "loopback")

    ctx.add_shutdown_callback(write_summary)

    if job.loopback:
        identity, kind = await find_helpline(ctx), rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    else:
        identity, kind = config.HELPLINE_IDENTITY, rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    await session.start(
        agent=agent,
        room=ctx.room,
        record=False,
        room_options=room_io.RoomOptions(participant_identity=identity, participant_kinds=[kind]),
    )

    # Listen before dialling, so the greeting is classified from its first word.
    async with AMD(session, llm=config.LLM, stt=None, participant_identity=identity, ivr_detection=False) as amd:
        if job.loopback:
            log.step("answered", line="mock helpline in the same room (loopback)")
        else:
            log.step("dialing", to=masked(job.phone_number), claim_number=job.claim_number)
            for attempt in (1, 2):
                try:
                    await ctx.api.sip.create_sip_participant(
                        api.CreateSIPParticipantRequest(
                            room_name=ctx.room.name,
                            sip_trunk_id=config.OUTBOUND_TRUNK_ID,
                            sip_call_to=job.phone_number,
                            participant_identity=identity,
                            participant_name="Insurer helpline",
                            wait_until_answered=True,
                        )
                    )
                    break
                except api.TwirpError as exc:
                    log.step("no_answer", sip_status=exc.metadata.get("sip_status_code"), detail=exc.message)
                    if attempt == 1 and worth_redialing(exc.message):
                        log.step("redialing", after_seconds=REDIAL_SECONDS)
                        await asyncio.sleep(REDIAL_SECONDS)
                        continue
                    # Close the room as a hang-up does: shutting down alone left anyone watching the call waiting
                    # out the time limit (a refused SIP login, 22 Oct 2026).
                    await agent.hang_up("no answer")
                    ctx.shutdown("no answer")
                    return
            log.step("answered", line="phone")
        verdict = await amd.execute()
    await agent.answered_by(verdict.category.value, getattr(verdict, "transcript", "") or "")

    async def give_up_after_max() -> None:
        await asyncio.sleep(config.MAX_CALL_SECONDS)
        if not agent.finished:
            await agent.hand_off("too_long", f"no status after {int(config.MAX_CALL_SECONDS)} s")

    asyncio.create_task(give_up_after_max())


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=config.CALLER_AGENT))
