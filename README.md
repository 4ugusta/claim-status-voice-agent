# Claim status voice agent

An outbound voice agent that calls an insurer's helpline, works through its phone menu, gives a claim
number, asks for the claim's status, and logs every step of the call.

```
 claim-caller (the agent)                                mock-insurer (the helpline)
 ────────────────────────                                ───────────────────────────
 dial out through a SIP trunk  ────── phone call ──────▶ answers on a phone number
 AMD: a menu, a person or voicemail?                     "For claims, press 1 ..."
 press 1                         ────── DTMF ─────────▶  claims
 key 48213#                      ────── DTMF ─────────▶  looks the claim up
 record "approved", checked      ◀───── speech ────────  "The status of this claim is: approved."
 against what was heard
 hang up, write the summary
```

## The task, step by step

| Task | Where |
|---|---|
| 1. A mock insurer helpline with a basic menu | `src/claimcall/helpline.py`: Northwind Health. Claims 1, billing 2, pharmacy 3, a representative 0. The claim number by keypad or voice, fixed statuses (`48213` approved, `51007` pending review, `66120` denied), re-prompts on silence and a goodbye after two. |
| 2. Navigate the menu by pressing the right option | `press_keys` sends DTMF. The model picks the option; the code checks the key against the options the menu actually read out. |
| 3. Give the claim number, ask for the status | `give_claim_number` keys it with `#` when the line asks; with a person, the agent says it in its opener. |
| 4. Capture the status and log each step | `record_claim_status` keeps the status and the sentence it came from, only if those words were heard. Every step is logged with its time. |
| Bonus: detect a live person | LiveKit's answering machine detection classifies the greeting (a menu, a person, voicemail). Mid call, a person who greets or offers help switches the agent from silent keys to talking. On the mock line, 0 reaches "Dana in claims", played by a model. |
| Bonus: hand off when stuck | Stuck is detected in code: the line repeats a prompt three times, 60 seconds pass without progress (silence included), or the call reaches 4 minutes. The agent dials `HANDOFF_PHONE_NUMBER` into the call, briefs the person and leaves them with the insurer. With no number set, it hangs up and logs that a person must call back. |

## Run it

Python 3.12, [uv](https://docs.astral.sh/uv/) and a LiveKit Cloud project. Speech to text (Deepgram Nova 3),
the model (GPT-4.1 mini) and text to speech (Cartesia Sonic 3) all run through LiveKit Inference on the
project's key, so for a test with no phone line that is the only account needed.

```bash
cp .env.example .env            # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
uv sync
uv run python -m claimcall.helpline dev     # terminal 1: the mock insurer
uv run python -m claimcall.caller dev       # terminal 2: the agent
uv run python scripts/call.py --loopback --claim 48213                          # terminal 3
uv run python scripts/call.py --loopback --claim 48213 --variant representative # no claims option: reach a person
```

`--loopback` puts both agents in one LiveKit room: the same code and the same DTMF events, with no telephony.

### A real phone call

LiveKit Phone Numbers only take calls in, so the helpline can have one, and the agent dials out through any
carrier's SIP trunk (Twilio Elastic SIP Trunking, Telnyx, ...).

```bash
uv run python scripts/setup_telephony.py helpline --livekit-number          # buys a number for the helpline
uv run python scripts/setup_telephony.py outbound --address <carrier SIP host> \
    --number <caller ID on that carrier> --username <user> --password <password>   # prints SIP_OUTBOUND_TRUNK_ID
uv run python scripts/call.py --to <the helpline number> --claim 48213
```

A LiveKit number is its own trunk, so the helpline's dispatch rule is limited to it by its id and never sees
calls meant for anything else in the project.

## What a call leaves behind

`scripts/call.py` prints each step as it happens, then writes:

- `logs/<call>.jsonl`, one line per step, e.g. `{"t": 13.18, "step": "answered_by", "category": "machine-ivr"}`.
  Steps: `dialing`, `answered`, `no_answer`, `answered_by`, `heard`, `said`, `pressed`, `claim_number_given`,
  `status_captured`, `live_person`, `stuck`, `handoff`, `hung_up`, and a closing `summary` with the status,
  the keys pressed, who answered and how long it took. Samples: [`logs/sample/`](logs/sample/).
- `logs/<call>.wav`, both sides in stereo, the agent on the left and the helpline on the right.

`scripts/render_call.py logs/<call>.jsonl` turns the two into a video: the call's own audio, with each step
shown at the moment it happened (`uv sync --group demo` adds Pillow; it needs ffmpeg).

## Design choices

- **LiveKit Agents for both ends.** One SDK covers SIP in and out, DTMF both ways, media and the agent loop,
  and its answering machine detection answers "who picked up". The same code runs a phone call and a loopback
  test.
- **The helpline is a state machine, not a model.** Real insurer lines are fixed trees. Testing against
  something that adapts to the agent would hide its mistakes.
- **AMD listens before dialling,** so the greeting is classified from its first word. Voicemail ends the call.
- **The model decides; code holds it to the task.** Each rule below exists because a test call went wrong
  without it:
  - *Wait for the line.* After a key or the claim number, nothing else happens until the line answers.
    A model chained a "stuck" call two seconds before the status was read.
  - *Speech to text lags.* Words heard within two seconds of a key are the old prompt. Without this the agent
    pressed 3 on a stale "for pharmacy benefits".
  - *A key must be on the menu.* Keys are checked against the options read out since the last key ("for
    billing, press two" becomes `{"2": "billing"}`), not against the reason the model gives: once it pressed
    billing and called it "claims", and once pressed 0 again during "please hold".
  - *A status must have been said.* Its words, in order, must appear in what was heard since the claim number.
  - *The claim number waits to be asked for.* Keyed into a menu it is just keys: `51007#` once reached the
    representative by its 0.
  - *With a person, the script speaks.* The opener and the number are fixed lines, said once, after the person
    pauses. Left to the model they were said three times, over the person, while they looked the claim up.
    The model may answer only a question asked after its last line.
  - *Stuck is also silence,* which no turn reports: a watchdog nudges a quiet person and hands off after 60 s.
  - *A colleague hears only trusted words.* The hand-off briefing is the claim number and a fixed reason chosen in
    code. The model's wording and the insurer's words, either of which the far end can steer, go to the log only.
    And four triggers (repeats, silence, the time limit, the model) firing together dial the person once.
- **A stereo recording beside the log,** because a log can be wrong. During testing it said lines were spoken
  that never were; the agent's channel was silent. The recording showed it.

## Results

Loopback test calls on the final code, each a fresh call against the mock helpline:

| Call | Outcome | Time |
|---|---|---|
| Menu, claim 48213 | approved, keys `1` then `48213#` | 37.6 s |
| Menu, claim 51007 | pending review | 29.7 s |
| Menu, claim 66120 | denied | 38.9 s |
| Menu, claim 99999 (does not exist) | no status, a person needs to call back: nothing invented | 33.8 s |
| No claims option, claim 48213 | pressed `0`, a person detected, approved | 46.3 s |
| No claims option, claim 51007 | pending review, from the person | 39.0 s |
| No claims option, claim 66120 | denied, from the person | 62.7 s |

7 of 7 correct (22 Oct 2026). Every status was checked against the words heard. Two of the logs are in
[`logs/sample/`](logs/sample/).

## What I'd improve next

- **Remember each insurer's menu.** The path to claim status rarely changes. Keep the keys that worked per
  number and press ahead, falling back to the model when a prompt no longer matches.
- **Hold queues.** Real insurer lines hold for ten to forty minutes. Detect hold music, stay silent and cheap,
  and pick back up when a person or a prompt returns.
- **Structured answers.** Status, paid amount, dates and denial reasons with a schema and a confidence,
  pressing 9 to hear it again when unsure.
- **A proper warm transfer.** Brief the person privately before joining them to the insurer.
- **An evaluation suite in CI.** Loopback runs against menu variants (reordered options, misheard digits,
  hold music, a wrong number, voicemail), tracking the success rate and time to status per change.
- **PHI.** Claim numbers and statuses are health information: redact or encrypt them in logs and recordings.

## Tests

```bash
uv run pytest
```

The unit tests cover the checks above, the claim number parsing, the call log and the helpline's keys.
