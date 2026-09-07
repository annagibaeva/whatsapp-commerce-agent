from wca.rules.render import to_english
from wca.rules.schema import Comparison, Group


def test_a_single_comparison_reads_as_a_sentence():
    cond = Comparison(fact="service_category", op="eq", value="colour")
    assert to_english(cond) == "service category is colour"


def test_is_true_reads_without_a_value():
    assert to_english(Comparison(fact="is_first_colour_visit", op="is_true")) == (
        "first colour visit is true"
    )


def test_all_joins_with_AND():
    cond = Group(all=[
        Comparison(fact="service_category", op="eq", value="colour"),
        Comparison(fact="is_first_colour_visit", op="is_true"),
    ])
    assert to_english(cond) == "service category is colour AND first colour visit is true"


def test_any_joins_with_OR_and_nests_in_brackets():
    cond = Group(all=[
        Comparison(fact="service_category", op="eq", value="colour"),
        Group(any=[
            Comparison(fact="customer_age", op="lt", value=16),
            Comparison(fact="needs_guardian", op="is_true"),
        ]),
    ])
    assert to_english(cond) == (
        "service category is colour AND (customer age is less than 16 "
        "OR needs guardian is true)"
    )


def test_not_reads_as_NOT():
    cond = Group(**{"not": Comparison(fact="service_category", op="eq", value="colour")})
    assert to_english(cond) == "NOT (service category is colour)"
