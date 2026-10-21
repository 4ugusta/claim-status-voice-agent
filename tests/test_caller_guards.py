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
