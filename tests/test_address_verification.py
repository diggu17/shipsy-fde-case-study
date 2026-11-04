"""Part 10: text format, history, geocoder, and the WhatsApp status shown on the screen."""

import copy

import pytest

from conftest import SAMPLE_DATA, load_seed, sample_files
from dispatch_planner.address import AreaLookup, _missing_detail, _read_address, assess_shift, clean_history, load_areas
from dispatch_planner.geocoding import (
    CachedGeocoder,
    GeocodingError,
    GoogleGeocoder,
    NoMapService,
    NominatimGeocoder,
    geocoder_for,
)
from dispatch_planner.planner import build_stops
from dispatch_planner.upload import process_shift

AL_KARAMA = (25.2455, 55.3025)


def read(name):
    return (SAMPLE_DATA / f"{name}.csv").read_bytes()


@pytest.fixture(scope="module")
def settings():
    return load_seed("sahra-express")


@pytest.fixture(scope="module")
def areas(settings):
    return load_areas(settings, read("areas"))


@pytest.fixture(scope="module")
def events(settings, areas):
    return clean_history(settings, areas, read("delivery_history"))[0]


@pytest.fixture(scope="module")
def shift(settings):
    return process_shift(settings, sample_files())


@pytest.fixture(scope="module")
def assessed(settings, areas, events, shift):
    rows = assess_shift(settings, shift["consignments"], areas, events, plan_date="2026-10-14")
    return {row["consignment_id"]: row for row in rows}


def missing(settings, areas, line_1, line_2="", area="Al Karama"):
    fields = {"address_line_1": line_1, "address_line_2": line_2, "area": area, "city": "Dubai", "notes": ""}
    return _missing_detail(settings, _read_address(settings, AreaLookup(settings, areas), fields))


class FakeGeocoder:
    def __init__(self, answer):
        self.answer = answer
        self.queries = []

    def locate(self, query):
        self.queries.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def geocoded(settings, areas, events, shift, consignment_id, geocoder):
    rows = [row for row in shift["consignments"] if row["id"] == consignment_id]
    return assess_shift(settings, rows, areas, events, map_service=geocoder, plan_date="2026-10-14")[0]


def answer(lat=AL_KARAMA[0], lng=AL_KARAMA[1], specific=True, full_match=True):
    return {"lat": lat, "lng": lng, "specific": specific, "full_match": full_match}


# Step 1: does the text look like an address?


@pytest.mark.parametrize("line_1", ["12345", "7", "00", "asdf 1", "Near Spinneys", "PO Box 78950", "Karama", "0", ""])
def test_text_that_isnt_an_address_needs_the_full_address(settings, areas, line_1):
    assert missing(settings, areas, line_1) == "full address"


def test_a_unit_number_with_too_many_digits_is_incomplete(settings, areas):
    assert missing(settings, areas, "Flat 99999", "Coral Tower") == "full address"


def test_a_flat_with_no_building_needs_the_building(settings, areas):
    assert missing(settings, areas, "Flat 1402") == "building name"


def test_a_building_with_no_unit_needs_the_unit(settings, areas):
    assert missing(settings, areas, "", "Coral Tower") == "flat or villa number"


def test_a_villa_or_warehouse_with_no_street_needs_the_street(settings, areas):
    assert missing(settings, areas, "Villa 5") == "street number"
    assert missing(settings, areas, "Warehouse 5") == "street number"


@pytest.mark.parametrize(
    "line_1, line_2",
    [
        ("Flat 1402", "Bin Hamoodha Building"),
        ("F-2812", "Al Shams Tower"),
        ("#813", "Lotus Bldg"),
        ("Flat no 112", "Al Hikma Apartments"),
        ("Office 320, Sandstone Design", "Coral Court"),
        ("شقة 108", "Coral Tower"),
        ("Villa 64, Street 39", ""),
        ("V27 street 18", ""),
        ("فيلا 96 St 10", ""),
        ("Warehouse 5, Street 19", ""),
        ("Makani 94542 87989", "Al Murad Tower"),
    ],
)
def test_complete_addresses_pass(settings, areas, line_1, line_2):
    assert missing(settings, areas, line_1, line_2) is None


def test_a_short_location_code_is_incomplete(settings, areas):
    assert missing(settings, areas, "Makani 123", "Al Murad Tower") == "full address"


def test_a_number_inside_the_area_name_isnt_a_unit(assessed):
    assert assessed["C0380"]["missing"] == "full address"


def test_a_floor_isnt_a_building(assessed):
    assert assessed["C0429"]["building"] == ""


# Step 2: past deliveries


def test_history_settles_an_area_name_with_several_matches(assessed):
    assert assessed["C0234"]["area"] == "Al Mankhool"


def test_an_area_history_cant_settle_is_asked_about(assessed):
    row = assessed["C0272"]
    assert row["area"] is None
    assert row["verification"]["question"] == "Is it Al Nahda 1 or Al Nahda (Sharjah)?"


def test_a_repeated_failure_warns_and_keeps_the_category(assessed):
    row = assessed["C0389"]
    assert row["quality"] == "GOOD"
    assert any(f["code"] == "repeated_address_failures" for f in row["findings"])
    assert row["action"] == "Driver calls the customer on arrival"


def test_address_not_found_events_are_never_used_as_locations(settings, areas):
    data = (
        b"consignment_id,date,address_line_1,address_line_2,area,city,landmark,customer_phone,outcome,failure_reason,event_lat,event_lng\n"
        b"C1,2026-01-01,Flat 1,Coral Tower,Al Karama,Dubai,,+971500,FAILED,ADDRESS_NOT_FOUND,25.2,55.3\n"
    )
    events, report = clean_history(settings, areas, data)
    assert report["kept"] == 0
    rows = [{"id": "X", "result": "accepted", "values": {"customer_phone": "+971500", "address_line_1": "Flat 1", "address_line_2": "Coral Tower", "area": "Al Karama", "city": "Dubai", "notes": ""}}]
    assert assess_shift(settings, rows, areas, events)[0]["source"] == "area_centre"


# Step 3: geocoder


def test_trusted_history_doesnt_call_the_geocoder(settings, areas, events, shift):
    geocoder = FakeGeocoder(answer())
    row = geocoded(settings, areas, events, shift, "C0104", geocoder)
    assert row["quality"] == "GOOD" and row["source"] != "map_service"
    assert geocoder.queries == []
    assert row["verification"] is None


def test_incomplete_text_doesnt_call_the_geocoder(settings, areas, events, shift):
    geocoder = FakeGeocoder(answer())
    row = geocoded(settings, areas, events, shift, "C0295", geocoder)
    assert geocoder.queries == []
    assert row["geocoding"] == "skipped_incomplete_text"
    assert row["verification"]["status"] == "whatsapp_triggered"


def test_no_known_area_doesnt_call_the_geocoder(settings, areas, events, shift):
    geocoder = FakeGeocoder(answer())
    row = geocoded(settings, areas, events, shift, "C0062", geocoder)
    assert geocoder.queries == []
    assert row["verification"]["question"] == "Which area is Al Manal Residence in?"


def test_the_geocoder_gets_no_phone_or_flat_number(settings, areas, events, shift):
    geocoder = FakeGeocoder(None)
    geocoded(settings, areas, events, shift, "C0091", geocoder)
    assert geocoder.queries == [
        {"place": "bin hamoodha building", "area": "Al Karama", "region": "Dubai", "country_code": "AE"}
    ]


def test_a_result_passing_all_checks_is_accepted(settings, areas, events, shift):
    row = geocoded(settings, areas, events, shift, "C0091", FakeGeocoder(answer()))
    assert (row["quality"], row["source"], row["confidence"]) == ("GOOD", "map_service", 0.7)
    assert row["action"] == "Driver calls the customer on arrival"
    assert row["verification"] is None


@pytest.mark.parametrize(
    "result, reason",
    [
        (answer(specific=False), "rejected_not_specific"),
        (answer(full_match=False), "rejected_partial_match"),
        (answer(lat=25.32), "rejected_too_far"),
    ],
)
def test_a_result_failing_a_check_keeps_the_area_centre(settings, areas, events, shift, result, reason):
    row = geocoded(settings, areas, events, shift, "C0091", FakeGeocoder(result))
    assert row["geocoding"] == reason
    assert row["source"] == "area_centre"
    assert row["verification"]["status"] == "whatsapp_triggered"


def test_a_result_on_the_area_centre_is_rejected(settings, areas, events, shift):
    centre = next(a for a in areas if a["area_name"] == "Al Karama")
    row = geocoded(settings, areas, events, shift, "C0091", FakeGeocoder(answer(lat=centre["lat"], lng=centre["lng"])))
    assert row["geocoding"] == "rejected_area_centre"
    assert row["source"] == "area_centre"


def test_a_service_error_keeps_the_area_centre(settings, areas, events, shift):
    row = geocoded(settings, areas, events, shift, "C0091", FakeGeocoder(GeocodingError("down")))
    assert row["geocoding"] == "service_error"
    assert row["source"] == "area_centre"
    assert row["routable"]


# Providers


def test_nominatim_maps_a_building_result():
    body = [{"lat": "25.2", "lon": "55.3", "place_rank": 30, "display_name": "Bin Hamoodha Building, Al Karama, Dubai"}]
    query = {"place": "bin hamoodha building", "area": "Al Karama", "region": "Dubai", "country_code": "AE"}
    assert NominatimGeocoder(lambda url: body).locate(query) == {"lat": 25.2, "lng": 55.3, "specific": True, "full_match": True}


def test_nominatim_flags_an_area_result_and_a_partial_name():
    body = [{"lat": "25.2", "lon": "55.3", "place_rank": 18, "display_name": "Al Karama, Dubai"}]
    query = {"place": "bin hamoodha building", "area": "Al Karama", "region": "Dubai", "country_code": "AE"}
    result = NominatimGeocoder(lambda url: body).locate(query)
    assert (result["specific"], result["full_match"]) == (False, False)


QUERY = {"place": "x", "area": "Al Karama", "region": "Dubai", "country_code": "AE"}


def google(result):
    return GoogleGeocoder("key", lambda url: {"status": "OK", "results": [result]}).locate(QUERY)


def test_google_maps_a_rooftop_result():
    body = {"geometry": {"location": {"lat": 25.2, "lng": 55.3}, "location_type": "ROOFTOP"}, "types": ["premise"]}
    assert google(body) == {"lat": 25.2, "lng": 55.3, "specific": True, "full_match": True}


def test_google_treats_a_centre_as_specific_only_on_a_building():
    location = {"lat": 25.2, "lng": 55.3}
    assert google({"geometry": {"location": location, "location_type": "GEOMETRIC_CENTER"}, "types": ["premise"]})["specific"]
    assert not google({"geometry": {"location": location, "location_type": "GEOMETRIC_CENTER"}, "types": ["neighborhood"]})["specific"]
    assert not google({"geometry": {"location": location, "location_type": "APPROXIMATE"}, "types": ["premise"]})["specific"]


def test_google_flags_a_partial_match():
    body = {"geometry": {"location": {"lat": 1, "lng": 2}, "location_type": "ROOFTOP"}, "partial_match": True}
    assert not google(body)["full_match"]


def test_google_finding_nothing_differs_from_an_error():
    assert GoogleGeocoder("key", lambda url: {"status": "ZERO_RESULTS"}).locate(QUERY) is None
    with pytest.raises(GeocodingError):
        GoogleGeocoder("key", lambda url: {"status": "OVER_QUERY_LIMIT"}).locate(QUERY)


def test_a_missing_google_key_means_no_provider(monkeypatch, settings):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("DISPATCH_GEOCODER", raising=False)
    google_settings = copy.deepcopy(settings)
    google_settings["address"]["verification"]["geocoder"] = "google"
    assert isinstance(geocoder_for(google_settings, None), NoMapService)


class Provider:
    min_interval_s = 0

    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def locate(self, query):
        self.calls += 1
        if self.error:
            raise self.error
        return answer()


def test_a_repeat_run_makes_no_new_calls(db):
    provider = Provider()
    first = CachedGeocoder(provider, "fake", db["geocode_cache"]).locate(QUERY)
    second = CachedGeocoder(provider, "fake", db["geocode_cache"]).locate(QUERY)
    assert first == second
    assert provider.calls == 1


def test_three_errors_in_a_row_stop_the_calls(db):
    provider = Provider(GeocodingError("down"))
    geocoder = CachedGeocoder(provider, "fake", db["geocode_cache"])
    for index in range(5):
        with pytest.raises(GeocodingError):
            geocoder.locate({**QUERY, "place": str(index)})
    assert provider.calls == 3


# Step 4: WhatsApp status


def test_every_approximate_parcel_gets_a_status_and_no_trusted_one_does(assessed):
    approximate = {"area_centre", "disagreeing_history", "phone_only", "same_phone_today", "no_location"}
    for row in assessed.values():
        assert bool(row["verification"]) == (row["source"] in approximate)


def test_the_message_asks_to_confirm_the_date_and_whats_missing(assessed):
    message = assessed["C0051"]["verification"]["message"]
    assert "C0051" in message and "2026-10-14" in message
    assert "confirm the delivery date" in message
    assert "full address" in message


def test_a_parcel_with_no_phone_needs_an_agent(assessed):
    assert assessed["C0159"]["verification"]["status"] == "needs_agent"


# What the plan does


def test_a_parcel_with_no_location_is_unassigned_with_a_next_step(settings, shift, assessed):
    built = build_stops(settings, {**shift, "hub_id": "AQ3", "plan_date": "2026-10-14"}, list(assessed.values()))
    reasons = {u["consignment_id"]: u["reason"] for u in built["unassigned"]}
    assert "moves to tomorrow" in reasons["C0051"]


def test_a_parcel_with_a_rough_location_still_goes_to_the_solver(settings, shift, assessed):
    built = build_stops(settings, {**shift, "hub_id": "AQ3", "plan_date": "2026-10-14"}, list(assessed.values()))
    reasons = {u["consignment_id"] for u in built["unassigned"]}
    assert assessed["C0091"]["verification"]["status"] == "whatsapp_triggered"
    assert "C0091" not in reasons
