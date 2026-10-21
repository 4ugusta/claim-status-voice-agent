"""The mock line's keys: a key that means nothing where the caller is must not cut off what the line is saying."""

from claimcall.helpline import MENU, MENU_NO_CLAIMS, Menu


def at(state: str, digits: str = "") -> Menu:
    m = Menu()
    m.state, m.digits = state, digits
    return m


def test_in_the_status_only_repeat_and_back_mean_anything():
    # a "#" after five digits once arrived during the status and silenced it
    assert at("status")._means_something("9") and at("status")._means_something("*")
    assert not at("status")._means_something("#") and not at("status")._means_something("4")


def test_while_collecting_the_claim_number():
    assert at("claim")._means_something("4")
    assert not at("claim")._means_something("#")  # nothing keyed yet
    assert at("claim", "48213")._means_something("#")
    assert not at("claim")._means_something("*")


def test_every_key_means_something_in_the_menu():
    assert all(at("menu")._means_something(k) for k in "0123456789*#")


def test_the_representative_variant_has_no_claims_option():
    assert "claims, press 1" in MENU and "claims" not in MENU_NO_CLAIMS
