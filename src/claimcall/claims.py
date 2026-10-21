"""The mock insurer's claims, and turning claim numbers into speech and back."""

import re

# Fixed statuses, as the task allows ("read back a fixed status like 'Your claim is approved'").
CLAIMS = {
    "48213": "approved",
    "51007": "pending review",
    "66120": "denied",
}
CLAIM_DIGITS = 5

# Number words only: "for", "to" and "ate" would turn "the claim number for this patient" into digits.
_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def status_of(claim_number: str) -> str | None:
    return CLAIMS.get(claim_number)


def digits_in(text: str) -> str:
    """The digits someone said or a speech recogniser wrote: "48213", "4 8 2 1 3" and "four eight two one
    three" all give "48213". "double" and "triple" repeat the next digit, as people say them."""
    out, repeat = [], 1
    for token in re.findall(r"[a-z]+|\d", text.lower()):
        if token in ("double", "triple"):
            repeat = 2 if token == "double" else 3
            continue
        digit = token if token.isdigit() else _WORDS.get(token)
        if digit is not None:
            out.append(digit * repeat)
        repeat = 1
    return "".join(out)


def spoken(claim_number: str) -> str:
    """A claim number read out digit by digit, the way a phone line says it."""
    return ", ".join(claim_number)
