"""The checks in code that hold the model to the task. Each one exists because a test call went wrong without it."""

import pytest

from claimcall import caller
from claimcall.caller import CallJob


def test_a_status_must_have_been_said_on_the_line():
    heard = ["Claim number five one zero zero seven.", "The status of this claim is pending review.", "Press nine."]
    assert caller.stated_on_line("pending review", heard)
    assert caller.stated_on_line("Pending Review.", heard)
    assert not caller.stated_on_line("approved", heard)  # a guess
    assert not caller.stated_on_line("review pending", heard)  # the words, in the order they were said
    assert not caller.stated_on_line("", heard)


def test_stuck_is_the_line_repeating_itself_or_time_passing_never_fragments():
    menu = ["Thank you for calling Northwind Health.", "For claims,", "press one.", "For billing,", "press two."]
    assert caller.stuck_reason(menu, seconds=10) is None  # one menu arrives as five fragments
    assert caller.stuck_reason(["For claims, press one."] * 3, seconds=10)
    assert caller.stuck_reason(["Hold on."], seconds=61)
    assert caller.stuck_reason(["ok", "ok", "ok"], seconds=10) is None  # too short to be a prompt


def test_a_person_greets_or_offers_help_a_menu_reads_options():
    assert caller.sounds_like_person("Thanks for holding, this is Dana in claims. How can I help you today?")
    assert not caller.sounds_like_person("The status of this claim is denied.")
    assert not caller.sounds_like_person("Hello, thank you for calling Northwind Health. For claims, press 1.")


def test_giving_up_early_needs_the_line_to_report_a_failure():
    assert caller.line_reports_failure(["We could not find claim number 9, 9, 9, 9, 9."])
    assert caller.line_reports_failure(["Sorry, that is not a valid option."])
    assert not caller.line_reports_failure(["Claim number four eight two one three,"])


def test_the_claim_number_waits_to_be_asked_for():
    assert caller.asks_for_number(["Please enter your five digit claim number on your keypad."])
    assert caller.asks_for_number(["Sure, what's the claim number?"])
    assert not caller.asks_for_number(["Thanks for holding. How can I help you today?"])


@pytest.mark.parametrize(
    "why, ok",
    [
        ("claims", True),
        ("claim status", True),
        ("speak with a representative", True),
        ("operator", True),
        ("repeat the menu", True),
        ("billing", False),
        ("pharmacy benefits", False),
    ],
)
def test_only_options_this_agent_needs(why, ok):
    assert bool(caller.USEFUL_OPTION.search(why)) is ok


def test_a_job_needs_a_claim_and_a_number_to_dial():
    job = CallJob.from_metadata('{"claim_number": "48213", "phone_number": "+15551234567"}')
    assert (job.claim_number, job.phone_number, job.loopback) == ("48213", "+15551234567", False)
    assert CallJob.from_metadata('{"claim_number": "4 8 2 1 3", "loopback": true}').claim_number == "48213"
    with pytest.raises(ValueError, match="claim_number"):
        CallJob.from_metadata('{"phone_number": "+15551234567"}')
    with pytest.raises(ValueError, match="E.164"):
        CallJob.from_metadata('{"claim_number": "48213", "phone_number": "555-1234"}')


def test_numbers_in_logs_keep_only_the_last_four():
    assert caller.masked("+15551234567") == "+1••••••4567"
    assert caller.masked("") == ""


def test_keys_are_judged_on_what_the_menu_said_not_on_the_models_reason():
    heard = [
        "Thank you for calling Northwind Health. For billing, press two.",
        "For pharmacy benefits, press three.",
        "To speak with a representative, press zero.",
    ]
    assert caller.menu_options(heard) == {"2": "billing", "3": "pharmacy benefits", "0": "speak with a representative"}
    assert caller.menu_options(["For claims,", "press one."]) == {"1": "claims"}
    assert caller.menu_options(["To hear this again, press nine. To return to the main menu, press star."]) == {
        "9": "hear this again",
        "*": "return to the main menu",
    }


def test_what_the_model_says_is_fit_to_say():
    assert caller.speakable("(Wait silently)") == ""
    assert caller.speakable("The claim is 48213, thanks.") == "The claim is 4, 8, 2, 1, 3, thanks."
    assert caller.asks_something("Can I get the claim number?") and caller.asks_something("Is that all?")
    assert not caller.asks_something("Let me look that up for you.")


def test_the_handoff_briefing_never_carries_text_from_the_call():
    # whatever the model or the insurer's line says, a colleague only ever hears one of these fixed reasons
    for brief in caller.HANDOFF_REASONS.values():
        assert "{" not in brief and len(brief) < 80
    assert set(caller.HANDOFF_REASONS) >= {"repeating", "no_progress", "too_long", "line_failed", "no_status"}


async def test_two_handoffs_at_once_dial_the_person_once(tmp_path, monkeypatch):
    # the stuck check, the silence watchdog, the time limit and the tool can all fire together
    import asyncio
    from types import SimpleNamespace

    from claimcall import config
    from claimcall.calllog import CallLog

    monkeypatch.setattr(config, "HANDOFF_NUMBER", "+15550000009")
    monkeypatch.setattr(config, "OUTBOUND_TRUNK_ID", "ST_test")
    dials = []

    async def slow_dial(request):
        dials.append(request.sip_call_to)
        await asyncio.sleep(0.05)  # the person's phone ringing

    ctx = SimpleNamespace(
        room=SimpleNamespace(name="room"), api=SimpleNamespace(sip=SimpleNamespace(create_sip_participant=slow_dial))
    )
    agent = caller.ClaimCaller(
        CallJob(claim_number="48213", phone_number="+15551234567"), CallLog("call", tmp_path), ctx
    )
    spoken = []
    monkeypatch.setattr(agent, "say", lambda text, **_: spoken.append(text))
    await asyncio.gather(agent.hand_off("no_progress", "watchdog"), agent.hand_off("too_long", "timer"))
    assert dials == ["+15550000009"]
    assert len(spoken) == 1 and "48213" not in spoken[0] and "4, 8, 2, 1, 3" in spoken[0]


async def test_the_number_is_said_once_when_the_person_is_noted_and_the_number_asked_together(tmp_path, monkeypatch):
    # a SIP call on 22 Oct: one model turn noted the person and gave the number while the opener waited for a pause
    import asyncio
    from types import SimpleNamespace

    from claimcall.calllog import CallLog

    agent = caller.ClaimCaller(
        CallJob(claim_number="48213", phone_number="+15551234567"), CallLog("call", tmp_path), SimpleNamespace()
    )
    spoken = []
    monkeypatch.setattr(agent, "say", lambda text, **_: spoken.append(text))

    async def a_pause():
        await asyncio.sleep(0.05)  # the person finishing "how can I help you today?"

    monkeypatch.setattr(agent, "wait_for_quiet", a_pause)
    opener = asyncio.create_task(agent.greet_person("conversation", "This is Dana in claims. How can I help you?"))
    await asyncio.sleep(0)
    told = await agent.give_claim_number(None, "voice")
    await opener
    assert "opener" in told
    assert sum("4, 8, 2, 1, 3" in line for line in spoken) == 1


def test_each_key_plays_its_own_two_tones():
    # the menu call's agent side was silent: keys went as events only, so nobody listening heard a press
    import numpy as np

    rate = 8000
    frames = caller.keypad_tones("5#", rate)
    samples = np.concatenate([np.frombuffer(f.data, dtype=np.int16) for f in frames])
    assert len(samples) == 2 * int(rate * caller.KEY_GAP_SECONDS)
    tone = samples[: int(rate * caller.TONE_SECONDS)].astype(float)
    spectrum, freqs = np.abs(np.fft.rfft(tone)), np.fft.rfftfreq(len(tone), 1 / rate)
    low, high = sorted(freqs[np.argsort(spectrum)[-2:]])
    assert abs(low - 770) < 10 and abs(high - 1336) < 10  # 5 is 770 Hz and 1336 Hz


def test_only_a_refused_login_or_a_timeout_is_dialled_again():
    assert caller.worth_redialing("no auth header in sip invite response (last response 401: Bad credentials)")
    assert caller.worth_redialing("sip request timed out")
    assert not caller.worth_redialing("INVITE failed: sip status: 404: No trunk found")
    assert not caller.worth_redialing("INVITE failed: sip status: 486: Busy Here")


async def test_a_call_with_its_status_is_never_handed_off_but_still_ends(tmp_path, monkeypatch):
    # a SIP call on 22 Oct: after "approved" was recorded, the model asked for a human "to complete the call
    # politely", and the log said the person could not give the status. A limit that fires must still hang up.
    from types import SimpleNamespace

    from claimcall.calllog import CallLog

    deleted = []

    async def delete_room(request):
        deleted.append(request.room)

    ctx = SimpleNamespace(
        room=SimpleNamespace(name="room"), api=SimpleNamespace(room=SimpleNamespace(delete_room=delete_room))
    )
    agent = caller.ClaimCaller(
        CallJob(claim_number="48213", phone_number="+15551234567"), CallLog("call", tmp_path), ctx
    )
    agent.person, agent.status = True, "approved"
    told = await agent.ask_for_human(None, "complete call politely")
    assert "end_call" in told and not agent.finished
    await agent.hand_off("too_long", "timer")  # the time limit
    steps = [line for line in (tmp_path / "call.jsonl").read_text().splitlines() if line]
    assert deleted == ["room"] and agent.finished
    assert not any('"step": "stuck"' in s or '"step": "handoff"' in s for s in steps)
