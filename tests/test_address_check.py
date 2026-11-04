import copy

import pytest

from conftest import SAMPLE_DATA, load_seed, sample_files
from dispatch_planner.address import (
    AreaLookup,
    assess_shift,
    clean_history,
    confirm_location,
    load_areas,
    score_accuracy,
)
from dispatch_planner.upload import process_shift


def read(name):
    return (SAMPLE_DATA / f"{name}.csv").read_bytes()


@pytest.fixture(scope="module")
def settings():
    return load_seed("sahra-express")


@pytest.fixture(scope="module")
def areas(settings):
    return load_areas(settings, read("areas"))


@pytest.fixture(scope="module")
def cleaned(settings, areas):
    return clean_history(settings, areas, read("delivery_history"))


@pytest.fixture(scope="module")
def shift(settings):
    return process_shift(settings, sample_files())


@pytest.fixture(scope="module")
def assessment_list(settings, areas, cleaned, shift):
    return assess_shift(settings, shift["consignments"], areas, cleaned[0])


@pytest.fixture(scope="module")
def assessments(assessment_list):
    return {a["consignment_id"]: a for a in assessment_list}


def rows_with_note(shift, phrase):
    return [r for r in shift["consignments"] if phrase in r["values"]["notes"].lower()]


def finding_codes(assessment):
    return [f["code"] for f in assessment["findings"]]


# Area names


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Al Baarha", "Al Baraha"),
        ("Deria", "Deira"),
        ("Al Kaarma", "Al Karama"),
        ("Interantional City", "International City"),
        ("JVC", "Jumeirah Village Circle"),
    ],
)
def test_a_misspelled_or_short_area_name_resolves_to_its_area(areas, settings, text, expected):
    assert AreaLookup(settings, areas).resolve(text)[0]["area_name"] == expected


@pytest.mark.parametrize("text", ["Nahda", "Bur Dubai", "Al Nahda"])
def test_a_name_that_matches_two_areas_is_not_resolved_by_name_alone(areas, settings, text):
    area, how = AreaLookup(settings, areas).resolve(text)

    assert area is None
    assert how == "multiple"


# Delivery history


def test_cleaning_the_history_fixes_and_removes_the_known_bad_events(cleaned):
    events, report = cleaned

    assert report["received"] == 5856
    assert report["swapped"] == 56
    assert report["at_hub_removed"] == 187
    assert report["unreliable_removed"] == 261
    assert report["kept"] == 5408
    assert sum(not event.get("location_unreliable") for event in events) == 5408
    assert all(event["lat"] < event["lng"] for event in events)


# Assessments


def test_every_row_is_assessed_except_rejected_ones(assessments):
    assert len(assessments) == 428
    assert "C0334" not in assessments


def test_a_customers_past_door_makes_an_address_good(assessments):
    row = assessments["C0104"]

    assert row["quality"] == "GOOD"
    assert row["source"] == "phone_and_area"
    assert row["confidence"] >= 0.85
    assert row["routable"] is True
    assert row["action_owner"] is None
    assert row["reason"] == "1 past delivery to this customer in The Greens"


def test_an_address_in_the_notes_is_recovered(assessments):
    assert assessments["C0242"]["quality"] == "GOOD"
    assert assessments["C0242"]["area"] == "Jumeirah Lakes Towers"


def test_a_vague_address_with_a_known_door_shows_the_past_address_to_the_driver(assessments):
    row = assessments["C0269"]

    assert row["quality"] == "GOOD"
    assert row["action_owner"] == "driver"
    assert row["past_address"]


def test_a_landmark_only_address_goes_to_customer_service(assessments):
    row = assessments["C0404"]

    assert row["quality"] == "INCOMPLETE"
    assert row["source"] == "area_centre"
    assert row["confidence"] == 0.2
    assert row["action_owner"] == "customer_service"
    assert row["message"]
    assert row["routable"] is True


def test_a_complete_address_with_no_history_is_left_to_the_driver(assessments):
    row = assessments["C0114"]

    assert row["quality"] == "INCOMPLETE"
    assert row["action_owner"] == "driver"
    assert row["message"] is None


def test_a_junk_address_can_be_rescued_by_the_phone_but_never_as_good(assessments):
    row = assessments["C0366"]

    assert row["quality"] == "AMBIGUOUS"
    assert row["source"] == "phone_only"
    assert row["confidence"] == 0.5
    assert row["action_owner"] == "customer_service"
    assert row["routable"] is True


def test_past_deliveries_that_disagree_are_not_trusted(assessments):
    row = assessments["C0033"]

    assert row["quality"] == "AMBIGUOUS"
    assert row["source"] == "disagreeing_history"
    assert row["confidence"] == 0.35


def test_a_building_with_no_area_has_no_location_and_cannot_be_routed(assessments):
    row = assessments["C0062"]

    assert row["quality"] == "AMBIGUOUS"
    assert (row["lat"], row["lng"]) == (None, None)
    assert row["routable"] is False
    assert row["action_owner"] == "customer_service"


def test_an_address_with_nothing_usable_is_invalid(assessments):
    row = assessments["C0051"]

    assert row["quality"] == "INVALID"
    assert row["confidence"] == 0
    assert row["routable"] is False
    assert row["message"]


def test_an_area_centre_is_never_good(assessment_list):
    assert all(a["quality"] != "GOOD" for a in assessment_list if a["source"] == "area_centre")


def test_a_phone_alone_is_never_good(assessment_list):
    assert all(a["quality"] != "GOOD" for a in assessment_list if a["source"] == "phone_only")


def test_a_blank_phone_is_never_used_as_evidence(assessment_list):
    blank = [a for a in assessment_list if a["consignment_id"] in ("C0344", "C0264", "C0159")]

    assert len(blank) == 3
    assert all(a["source"] not in ("phone_and_area", "phone_only", "same_phone_today") for a in blank)


def test_a_location_code_is_recorded(assessments):
    assert assessments["C0033"]["location_code"] == "9454287989"


def test_an_area_outside_the_service_area_is_flagged_and_still_routable(assessments):
    row = assessments["C0226"]

    assert "outside_service_area" in finding_codes(row)
    assert row["routable"] is True


@pytest.mark.parametrize("phrase", ["before noon", "after 6pm"])
def test_a_note_that_conflicts_with_the_booked_slot_is_flagged(shift, assessments, phrase):
    rows = rows_with_note(shift, phrase)
    conflicting = [
        r for r in rows
        if (phrase == "before noon" and r["values"]["slot_start"] and r["values"]["slot_start"] >= "12:00")
        or (phrase == "after 6pm" and r["values"]["slot_end"] and r["values"]["slot_end"] <= "18:00")
    ]

    assert conflicting
    for row in conflicting:
        assert "note_conflicts_with_slot" in finding_codes(assessments[row["id"]])


def test_a_note_that_fits_the_booked_slot_is_not_flagged(shift, assessments):
    fitting = [r for r in rows_with_note(shift, "before noon") if r["values"]["slot_start"] == "09:00"]

    assert fitting
    for row in fitting:
        assert "note_conflicts_with_slot" not in finding_codes(assessments[row["id"]])


def test_a_conflicting_note_never_changes_the_booked_slot(shift, assessments):
    row = next(r for r in rows_with_note(shift, "before noon") if r["values"]["slot_start"] == "13:00")

    assert row["values"]["slot_start"] == "13:00"
    assert assessments[row["id"]]["findings"]


@pytest.mark.parametrize("phrase", ["contactless", "leave with security"])
def test_a_cod_order_that_cannot_be_left_is_flagged(shift, assessments, phrase):
    rows = [r for r in rows_with_note(shift, phrase) if r["values"]["payment_type"] == "COD"]

    assert rows
    for row in rows:
        assert "note_conflicts_with_cod" in finding_codes(assessments[row["id"]])


def test_confirming_a_location_makes_it_good_and_routable(settings, assessments):
    confirmed = confirm_location(settings, copy.deepcopy(assessments["C0051"]), 25.2, 55.3)

    assert confirmed["quality"] == "GOOD"
    assert confirmed["confidence"] == 1.0
    assert confirmed["source"] == "confirmed"
    assert (confirmed["lat"], confirmed["lng"]) == (25.2, 55.3)
    assert confirmed["routable"] is True
    assert confirmed["action_owner"] is None


# Accuracy


def test_accuracy_beats_the_area_centre_baseline(settings, assessment_list):
    result = score_accuracy(settings, assessment_list, read("ground_truth_sample"))

    assert result["overall"]["verified"] == 50
    assert result["overall"]["located"] >= 48
    assert result["overall"]["median_m"] < result["area_centre_baseline"]["median_m"]
    assert result["area_centre_baseline"]["median_m"] == pytest.approx(581, abs=30)


def test_every_good_address_in_the_verified_sample_is_close(settings, assessment_list):
    good = score_accuracy(settings, assessment_list, read("ground_truth_sample"))["by_quality"]["GOOD"]

    assert good["located"] >= 30
    assert good["within_100m"] == good["located"]
    assert good["worst_m"] <= 100


def test_changing_the_verified_file_changes_accuracy_but_no_assessment(settings, areas, cleaned, shift, assessment_list):
    before = copy.deepcopy(assessment_list)
    moved = read("ground_truth_sample").replace(b",25.", b",24.")

    first = score_accuracy(settings, assessment_list, read("ground_truth_sample"))
    second = score_accuracy(settings, assessment_list, moved)

    assert first != second
    assert assessment_list == before
    assert assess_shift(settings, shift["consignments"], areas, cleaned[0]) == before


def test_the_map_service_can_be_replaced_without_other_changes(settings, areas, cleaned, shift):
    centre = next(area for area in areas if area["area_name"] == "The Greens")

    class FakeMapService:
        def locate(self, query):
            found = query["place"] == "al mina tower"
            return {"lat": centre["lat"] + 0.005, "lng": centre["lng"], "specific": True, "full_match": True} if found else None

    rows = [r for r in shift["consignments"] if r["id"] == "C0114"]
    row = assess_shift(settings, rows, areas, cleaned[0], map_service=FakeMapService())[0]

    assert row["source"] == "map_service"
    assert (row["lat"], row["lng"]) == (centre["lat"] + 0.005, centre["lng"])
    assert row["quality"] == "GOOD"


