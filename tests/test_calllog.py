import json

import pytest

from claimcall.calllog import CallLog, describe


def clock(times):
    """The given times, then the last one again: a summary reads the clock once more than its steps."""
    it = iter(times)
    last = [0.0]

    def now():
        last[0] = next(it, last[0])
        return last[0]

    return now


def test_every_step_is_timed_written_and_published(tmp_path):
    published = []
    log = CallLog("call-1", tmp_path, publish=published.append, clock=clock([100.0, 101.5, 103.25]))
    log.step("pressed", keys="1", why="claims")
    log.step("status_captured", status="approved", verbatim="The status of this claim is approved.")
    lines = [json.loads(line) for line in (tmp_path / "call-1.jsonl").read_text().splitlines()]
    assert [(e["t"], e["step"]) for e in lines] == [(1.5, "pressed"), (3.25, "status_captured")]
    assert [json.loads(p)["step"] for p in published] == ["pressed", "status_captured"]


def test_an_unknown_step_is_refused(tmp_path):
    with pytest.raises(ValueError):
        CallLog("call-2", tmp_path).step("guessed")


def test_summary_gathers_the_call(tmp_path):
    log = CallLog("call-3", tmp_path, clock=clock([0, 1, 2, 3, 4, 5, 6]))
    log.step("answered_by", category="machine-ivr", greeting="For claims, press 1.")
    log.step("pressed", keys="1", why="claims")
    log.step("pressed", keys="48213#", why="claim number")
    log.step("status_captured", status="approved", verbatim="The status of this claim is approved.")
    s = log.summary(claim_number="48213")
    assert s["outcome"] == "status_found" and s["claim_status"] == "approved"
    assert s["keys_pressed"] == "148213#" and s["answered_by"] == "machine-ivr"
    assert s["live_person"] is False and s["handed_off"] is False


def test_summary_without_a_status(tmp_path):
    log = CallLog("call-4", tmp_path, clock=clock([0, 1, 2, 3]))
    log.step("stuck", reason="the claim was not found")
    log.step("handoff", result="no handoff number set")
    s = log.summary()
    assert s["outcome"] == "no_status" and s["claim_status"] is None and s["handed_off"] is True


def test_a_watcher_going_away_never_stops_the_call(tmp_path):
    def broken(_):
        raise ConnectionError("room closed")

    CallLog("call-5", tmp_path, publish=broken).step("hung_up", reason="done")


def test_describe_reads_like_a_log_line():
    assert describe({"t": 1, "at": "", "step": "heard", "text": "For claims, press 1."}) == "“For claims, press 1.”"
    assert describe({"t": 1, "at": "", "step": "pressed", "keys": "1", "why": "claims"}) == "1  (claims)"
