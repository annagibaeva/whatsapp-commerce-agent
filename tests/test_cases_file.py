from collections import Counter

from wca.cases import load_cases

CASES = load_cases("cases/v0.cases.json")


def test_there_are_twenty_one_cases():
    # Was twenty. clean_05 (a plain cut booking) was added once cut_allowed
    # made cuts bookable -- see policy/salon.rules.json and cases/v0.cases.json.
    assert len(CASES.cases) == 21


def test_every_tier_has_four_cases_except_clean_which_has_five():
    # clean gained clean_05 for the new cut_allowed rule; every other tier
    # is unchanged at four.
    counts = Counter(c.tier for c in CASES.cases)
    assert counts == {
        "clean": 5, "adversarial": 4, "override": 4, "unanswerable": 4, "ambiguous": 4
    }


def test_case_ids_are_unique():
    ids = [c.id for c in CASES.cases]
    assert len(ids) == len(set(ids))


def test_every_case_has_a_note_explaining_itself():
    for case in CASES.cases:
        assert len(case.note) > 20, case.id


def test_a_blocked_case_names_the_check_it_should_fail():
    for case in CASES.cases:
        if not case.expect_allowed:
            assert case.expect_check, f"{case.id} expects a block but names no check"


def test_a_passing_case_names_no_check():
    for case in CASES.cases:
        if case.expect_allowed:
            assert case.expect_check is None, case.id


def test_bad_bookings_are_only_marked_on_blocked_booking_cases():
    for case in CASES.cases:
        if case.would_be_bad_booking:
            assert case.expect_action == "book"
            assert case.expect_allowed is False


def test_at_least_one_case_would_be_a_bad_booking():
    # Without this, running with the gate off proves nothing.
    assert any(c.would_be_bad_booking for c in CASES.cases)
