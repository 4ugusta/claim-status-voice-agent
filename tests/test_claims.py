from claimcall import claims


def test_digits_from_numerals_spaced_digits_and_words():
    assert claims.digits_in("48213") == "48213"
    assert claims.digits_in("4 8 2 1 3") == "48213"
    assert claims.digits_in("four eight two one three") == "48213"
    assert claims.digits_in("five one oh oh seven") == "51007"
    assert claims.digits_in("six double six one two zero") == "666120"


def test_ordinary_words_are_not_digits():
    # "for", "to" and "ate" would turn a sentence into a wrong claim number
    assert claims.digits_in("the claim number for this patient is 48213") == "48213"
    assert claims.digits_in("I want to check a claim") == ""


def test_spoken_reads_digit_by_digit():
    assert claims.spoken("48213") == "4, 8, 2, 1, 3"


def test_fixed_statuses():
    assert claims.status_of("48213") == "approved"
    assert claims.status_of("51007") == "pending review"
    assert claims.status_of("99999") is None
