import pytest

from conftest import load_seed, sample_files
from dispatch_planner.upload import UploadRejected, process_shift


@pytest.fixture(scope="module")
def shift():
    return process_shift(load_seed("sahra-express"), sample_files())


@pytest.fixture(scope="module")
def consignments(shift):
    return {row["id"]: row for row in shift["consignments"]}


@pytest.fixture(scope="module")
def drivers(shift):
    return {row["id"]: row for row in shift["drivers"]}


@pytest.fixture(scope="module")
def vehicles(shift):
    return {row["id"]: row for row in shift["vehicles"]}


def replace_in_csv(data, old, new):
    return data.decode("utf-8-sig").replace(old, new).encode()


# Consignments


@pytest.mark.parametrize("row_id", ["C0008", "C0272"])
def test_an_exact_duplicate_is_kept_once_and_reported(shift, consignments, row_id):
    assert [row["id"] for row in shift["consignments"]].count(row_id) == 1
    assert "duplicate_exact" in [f["code"] for f in consignments[row_id]["findings"]]


def test_a_conflicting_duplicate_keeps_the_last_row_and_is_flagged(shift, consignments):
    row = consignments["C0100"]

    assert [r["id"] for r in shift["consignments"]].count("C0100") == 1
    assert row["values"]["weight_kg"] == 4.33
    assert row["result"] == "flagged"
    assert "duplicate_conflict" in [f["code"] for f in row["findings"]]


def test_a_test_row_is_rejected(consignments):
    assert consignments["C0334"]["result"] == "rejected"
    assert consignments["C0334"]["plannable"] is False


@pytest.mark.parametrize("row_id", ["C0166", "C0033", "C0004"])
def test_an_impossible_weight_is_held(consignments, row_id):
    assert consignments[row_id]["result"] == "held"
    assert consignments[row_id]["plannable"] is False


@pytest.mark.parametrize(
    "row_id, suggested",
    [("C0119", "2.87"), ("C0165", "2.46"), ("C0415", "0.87"), ("C0321", "0.39")],
)
def test_a_weight_far_too_heavy_for_its_box_is_held_with_a_suggestion(consignments, row_id, suggested):
    row = consignments[row_id]

    assert row["result"] == "held"
    assert row["suggestion"] == {"weight_kg": suggested}


@pytest.mark.parametrize("row_id, weight", [("C0034", 61.6), ("C0032", 44.6), ("C0263", 59.3)])
def test_a_weight_only_somewhat_too_heavy_is_left_alone(consignments, row_id, weight):
    assert consignments[row_id]["values"]["weight_kg"] == weight
    assert consignments[row_id]["plannable"] is True


@pytest.mark.parametrize("row_id", ["C0367", "C0029", "C0078"])
def test_a_missing_box_size_is_flagged_with_the_default(consignments, row_id):
    row = consignments[row_id]

    assert row["result"] == "flagged"
    assert (row["values"]["length_cm"], row["values"]["width_cm"], row["values"]["height_cm"]) == (60, 40, 40)
    assert row["plannable"] is True


@pytest.mark.parametrize("row_id", ["C0026", "C0413", "C0303", "C0094", "C0300"])
def test_a_parcel_too_big_for_every_vehicle_is_held(consignments, row_id):
    row = consignments[row_id]

    assert row["result"] == "held"
    assert "too_big" in [f["code"] for f in row["findings"]]
    assert row["suggestion"] is None


def test_cod_over_the_cash_limit_is_flagged_and_cannot_be_planned(consignments):
    row = consignments["C0177"]

    assert row["result"] == "flagged"
    assert row["plannable"] is False
    assert "over_cash_limit" in [f["code"] for f in row["findings"]]


@pytest.mark.parametrize("row_id", ["C0168", "C0209", "C0083"])
def test_a_cod_field_error_is_flagged(consignments, row_id):
    assert consignments[row_id]["result"] == "flagged"
    assert "cod_error" in [f["code"] for f in consignments[row_id]["findings"]]


@pytest.mark.parametrize(
    "row_id, start, end",
    [
        ("C0115", "17:00", "21:00"),
        ("C0355", "09:00", "13:00"),
        ("C0358", "09:00", "13:00"),
        ("C0028", "09:00", "13:00"),
    ],
)
def test_am_pm_slots_are_corrected(consignments, row_id, start, end):
    row = consignments[row_id]

    assert (row["values"]["slot_start"], row["values"]["slot_end"]) == (start, end)
    assert "slot_format" in [f["code"] for f in row["findings"]]
    assert row["original"]["slot_start"] != start


@pytest.mark.parametrize(
    "row_id, start, end",
    [("C0374", "09:00", "13:00"), ("C0412", "17:00", "21:00"), ("C0332", "13:00", "17:00")],
)
def test_a_reversed_slot_is_flagged_and_planned_swapped(consignments, row_id, start, end):
    row = consignments[row_id]

    assert row["result"] == "flagged"
    assert (row["values"]["slot_start"], row["values"]["slot_end"]) == (start, end)


@pytest.mark.parametrize("row_id", ["C0327", "C0408"])
def test_a_slot_outside_delivery_hours_is_flagged_and_planned_as_any_time(consignments, row_id):
    row = consignments[row_id]

    assert row["result"] == "flagged"
    assert (row["values"]["slot_start"], row["values"]["slot_end"]) == (None, None)


@pytest.mark.parametrize(
    "row_id, start, end",
    [("C0296", "10:00", "10:45"), ("C0276", "15:30", "16:00"), ("C0161", "18:00", "18:30")],
)
def test_a_slot_that_is_not_offered_is_flagged_and_kept(consignments, row_id, start, end):
    row = consignments[row_id]

    assert row["result"] == "flagged"
    assert (row["values"]["slot_start"], row["values"]["slot_end"]) == (start, end)


@pytest.mark.parametrize("row_id", ["C0344", "C0264", "C0159"])
def test_a_blank_phone_is_flagged(consignments, row_id):
    assert consignments[row_id]["result"] in ("flagged", "held")
    assert "blank_phone" in [f["code"] for f in consignments[row_id]["findings"]]


def test_an_address_of_zero_passes_through_for_the_address_check(consignments):
    row = consignments["C0051"]

    assert row["values"]["address_line_1"] == "0"
    assert row["plannable"] is True


def test_an_unrecognised_payment_type_is_held():
    files = sample_files()
    files["consignments"] = replace_in_csv(files["consignments"], ",PREPAID,", ",UNKNOWN,")

    shift = process_shift(load_seed("sahra-express"), files)

    row = next(r for r in shift["consignments"] if r["id"] == "C0371")
    assert row["result"] == "held"
    assert "unknown_payment_type" in [f["code"] for f in row["findings"]]


# Drivers


def test_a_shift_outside_delivery_hours_excludes_the_driver(drivers):
    assert drivers["D14"]["result"] == "flagged"
    assert drivers["D14"]["plannable"] is False


def test_a_shift_over_the_working_limit_is_capped(drivers):
    row = drivers["D17"]

    assert row["result"] == "flagged"
    assert row["values"]["shift_end"] == "18:00"
    assert row["original"]["shift_end"] == "23:00"
    assert row["plannable"] is True


def test_a_known_licence_label_is_translated_quietly(drivers):
    assert drivers["D28"]["values"]["licence"] == "LIGHT_VEHICLE"
    assert drivers["D28"]["original"]["licence"] == "light"
    assert drivers["D28"]["result"] == "accepted"
    assert drivers["D28"]["findings"] == []
    assert drivers["D28"]["plannable"] is True


def test_a_driver_on_leave_is_accepted_but_not_planned(drivers):
    assert drivers["D01"]["result"] == "accepted"
    assert drivers["D01"]["plannable"] is False


def test_a_normal_driver_is_accepted_and_planned(drivers):
    assert drivers["D04"]["result"] == "accepted"
    assert drivers["D04"]["plannable"] is True


def test_an_unknown_licence_label_is_held():
    files = sample_files()
    files["drivers"] = replace_in_csv(files["drivers"], ",light,", ",HEAVY,")

    shift = process_shift(load_seed("sahra-express"), files)

    row = next(r for r in shift["drivers"] if r["id"] == "D28")
    assert row["result"] == "held"
    assert row["plannable"] is False


# Vehicles


def test_a_volume_that_differs_from_the_settings_uses_the_settings_value(vehicles):
    row = vehicles["V06"]

    assert row["result"] == "flagged"
    assert row["values"]["max_volume_m3"] == 3.0
    assert row["original"]["max_volume_m3"] == "30"
    assert row["plannable"] is True


def test_a_refrigerated_motorbike_is_treated_as_not_refrigerated(vehicles):
    assert vehicles["V01"]["result"] == "flagged"
    assert vehicles["V01"]["values"]["refrigerated"] is False


def test_a_known_vehicle_label_is_translated_quietly(vehicles):
    assert vehicles["V10"]["values"]["type"] == "SMALL_VAN"
    assert vehicles["V10"]["original"]["type"] == "Small Van"
    assert vehicles["V10"]["result"] == "accepted"
    assert vehicles["V10"]["findings"] == []


def test_a_vehicle_in_maintenance_is_accepted_but_not_planned(vehicles):
    assert vehicles["V05"]["result"] == "accepted"
    assert vehicles["V05"]["plannable"] is False


def test_an_unknown_vehicle_type_is_held():
    files = sample_files()
    files["vehicles"] = replace_in_csv(files["vehicles"], ",Small Van,", ",Lorry,")

    shift = process_shift(load_seed("sahra-express"), files)

    row = next(r for r in shift["vehicles"] if r["id"] == "V10")
    assert row["result"] == "held"


# Files


def test_a_missing_required_column_rejects_the_upload():
    files = sample_files()
    files["consignments"] = replace_in_csv(files["consignments"], "weight_kg", "mass")

    with pytest.raises(UploadRejected) as rejected:
        process_shift(load_seed("sahra-express"), files)

    assert rejected.value.problems == [
        {"path": "consignments.weight_kg", "message": "Missing column: weight_kg"}
    ]


def test_all_missing_columns_across_files_are_listed_together():
    files = sample_files()
    files["consignments"] = replace_in_csv(files["consignments"], "weight_kg", "mass")
    files["drivers"] = replace_in_csv(files["drivers"], "shift_end", "finish")

    with pytest.raises(UploadRejected) as rejected:
        process_shift(load_seed("sahra-express"), files)

    assert [p["path"] for p in rejected.value.problems] == ["consignments.weight_kg", "drivers.shift_end"]


def test_an_unreadable_file_rejects_the_upload():
    files = sample_files()
    files["vehicles"] = b"\xff\xfe\x00not text"

    with pytest.raises(UploadRejected) as rejected:
        process_shift(load_seed("sahra-express"), files)

    assert rejected.value.problems == [{"path": "vehicles", "message": "File can't be read"}]


def test_the_report_accounts_for_every_row_received(shift):
    report = shift["report"]["consignments"]

    assert report["received"] == 432
    assert report["duplicates_removed"] == 3
    assert (
        report["accepted"] + report["corrected"] + report["flagged"] + report["held"] + report["rejected"]
        == 429
    )
    assert shift["report"]["drivers"]["received"] == 28
    assert shift["report"]["vehicles"]["received"] == 24


def test_the_report_counts_match_the_known_bad_rows(shift):
    report = shift["report"]["consignments"]

    assert report["rejected"] == 1
    assert report["held"] == 12


def test_the_report_lists_a_message_for_every_row_with_a_finding(shift):
    issues = {(i["file"], i["id"]): i for i in shift["report"]["issues"]}

    assert issues[("consignments", "C0119")]["result"] == "held"
    assert issues[("consignments", "C0119")]["messages"]
    assert issues[("vehicles", "V06")]["result"] == "flagged"
    assert ("drivers", "D04") not in issues
