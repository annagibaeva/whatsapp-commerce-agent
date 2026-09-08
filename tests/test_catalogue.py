import json

import pytest
from pydantic import ValidationError

from wca.catalogue import Catalogue, Service, facts_for, load_catalogue


def test_the_real_catalogue_file_loads():
    cat = load_catalogue("policy/salon.catalogue.json")
    assert len(cat.services) == 3
    assert cat.get("svc_cut") is not None


def test_get_finds_a_known_service():
    cat = load_catalogue("policy/salon.catalogue.json")
    service = cat.get("svc_colour_full")
    assert service is not None
    assert service.name == "full head colour"
    assert service.category == "colour"
    assert service.price_minor == 18000


def test_get_misses_an_unknown_service():
    cat = load_catalogue("policy/salon.catalogue.json")
    assert cat.get("svc_does_not_exist") is None


def test_search_matches_on_name():
    cat = load_catalogue("policy/salon.catalogue.json")
    results = cat.search("cut")
    ids = {s.id for s in results}
    assert "svc_cut" in ids


def test_search_matches_on_category():
    cat = load_catalogue("policy/salon.catalogue.json")
    results = cat.search("colour")
    ids = {s.id for s in results}
    assert ids == {"svc_colour_full", "svc_colour_roots"}


def test_search_is_case_insensitive():
    cat = load_catalogue("policy/salon.catalogue.json")
    results = cat.search("COLOUR")
    assert len(results) == 2


def test_search_returns_empty_for_nonsense_rather_than_guessing():
    cat = load_catalogue("policy/salon.catalogue.json")
    assert cat.search("massage") == ()


def test_duplicate_service_ids_fail_to_load(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"services": [
        {"id": "svc_x", "name": "a", "category": "cut",
         "duration_minutes": 30, "price_minor": 1000},
        {"id": "svc_x", "name": "b", "category": "cut",
         "duration_minutes": 30, "price_minor": 1000},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalogue(bad)


def test_facts_for_returns_exactly_the_two_keys_the_rules_read():
    service = Service(
        id="svc_x", name="test service", category="colour",
        duration_minutes=60, price_minor=9000,
    )
    facts = facts_for(service)
    assert facts == {"service_category": "colour", "quoted_price_minor": 9000}


def test_service_and_catalogue_reject_unknown_fields():
    with pytest.raises(ValidationError):
        Service(
            id="svc_x", name="test", category="cut",
            duration_minutes=30, price_minor=1000, extra_field="nope",
        )
    with pytest.raises(ValidationError):
        Catalogue(services=(), extra_field="nope")
