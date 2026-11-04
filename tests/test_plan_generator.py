import copy
from collections import Counter

import pytest

from conftest import SAMPLE_DATA, load_seed, sample_files
from dispatch_planner.address import assess_shift, clean_history, load_areas
from dispatch_planner.plan_check import check_plan
from dispatch_planner.planner import build_export, build_stops, generate_plan, pair_drivers
from dispatch_planner.upload import process_shift


def read(name):
    return (SAMPLE_DATA / f"{name}.csv").read_bytes()


@pytest.fixture(scope="module")
def settings():
    settings = load_seed("sahra-express")
    settings["planning"]["solver_seconds"] = 5
    return settings


@pytest.fixture(scope="module")
def shift(settings):
    shift = process_shift(settings, sample_files())
    shift.update(client_id="sahra-express", hub_id="AQ3", plan_date="2026-10-14")
    return shift


@pytest.fixture(scope="module")
def assessments(settings, shift):
    areas = load_areas(settings, read("areas"))
    events, _ = clean_history(settings, areas, read("delivery_history"))
    return assess_shift(settings, shift["consignments"], areas, events)


@pytest.fixture(scope="module")
def generated(settings, shift, assessments):
    return generate_plan(settings, shift, assessments)


def plan_ids(plan):
    ids = Counter(c for route in plan["routes"] for stop in route["stops"] for c in stop["consignment_ids"])
    ids.update(entry["consignment_id"] for entry in plan["unassigned"])
    return ids


def route_of(plan, consignment_id):
    for route in plan["routes"]:
        for stop in route["stops"]:
            if consignment_id in stop["consignment_ids"]:
                return route
    return None


def test_a_real_plan_is_valid_according_to_the_independent_checker(settings, shift, assessments, generated):
    result = check_plan(settings, shift, assessments, generated["plan"])

    assert result["valid"] is True
    assert generated["method"] == "solver"


def test_every_row_appears_exactly_once(shift, generated):
    ids = plan_ids(generated["plan"])

    assert set(ids) == {row["id"] for row in shift["consignments"]}
    assert set(ids.values()) == {1}


def test_most_parcels_get_routed(generated, assessments):
    routable = sum(a["routable"] for a in assessments)
    routed = sum(len(s["consignment_ids"]) for r in generated["plan"]["routes"] for s in r["stops"])

    assert routed >= 0.75 * routable


@pytest.mark.parametrize(
    "row_id, phrase",
    [("C0177", "cash limit"), ("C0051", "moves to tomorrow"), ("C0026", "Needs a decision"), ("C0334", "Rejected")],
)
def test_rows_that_cannot_go_on_a_route_are_unassigned_with_a_reason(generated, row_id, phrase):
    entry = next(e for e in generated["plan"]["unassigned"] if e["consignment_id"] == row_id)

    assert phrase in entry["reason"]


def test_chilled_parcels_only_go_on_chilled_vans(shift, generated):
    chilled = [r["id"] for r in shift["consignments"] if r["values"].get("handling") == "CHILLED"]
    routes = {route_of(generated["plan"], c)["vehicle_id"] for c in chilled if route_of(generated["plan"], c)}

    assert routes <= {"V17"}


def test_motorcycle_only_drivers_only_get_motorbikes(shift, generated):
    only_bikes = {r["id"] for r in shift["drivers"] if r["values"]["licence"] == "MOTORCYCLE"}
    vehicles = {r["id"]: r["values"]["type"] for r in shift["vehicles"]}

    for route in generated["plan"]["routes"]:
        if route["driver_id"] in only_bikes:
            assert vehicles[route["vehicle_id"]] == "MOTORBIKE"


def test_parcels_for_one_place_with_different_slots_are_separate_stops(settings, shift, assessments):
    stops = build_stops(settings, shift, assessments)["stops"]
    together = [s for s in stops if {"C0131", "C0232"} <= set(s["consignment_ids"])]

    assert together == []


def test_parcels_for_one_place_with_the_same_slot_share_a_stop(settings, shift, assessments):
    stops = build_stops(settings, shift, assessments)["stops"]

    assert any({"C0232", "C0006"} <= set(s["consignment_ids"]) for s in stops)


def test_each_stop_has_only_one_kind_of_slot(settings, shift, assessments):
    rows = {r["id"]: r for r in shift["consignments"]}
    for stop in build_stops(settings, shift, assessments)["stops"]:
        slots = {rows[c]["values"]["slot_start"] for c in stop["consignment_ids"]} - {None}
        assert len(slots) <= 1


def test_pairing_gives_each_vehicle_one_driver_and_lists_who_is_left_out(settings, shift):
    pairs = pair_drivers(settings, shift)

    assert len(pairs["pairs"]) == 22
    assert len({p["driver_id"] for p in pairs["pairs"]}) == 22
    assert len(pairs["unused_drivers"]) == 2


def test_pairing_respects_licences(settings, shift):
    drivers = {r["id"]: r["values"]["licence"] for r in shift["drivers"]}
    vehicles = {r["id"]: r["values"]["type"] for r in shift["vehicles"]}
    allowed = {entry["licence_id"]: entry["vehicle_types"] for entry in settings["licences"]}

    for pair in pair_drivers(settings, shift)["pairs"]:
        assert vehicles[pair["vehicle_id"]] in allowed[drivers[pair["driver_id"]]]


def test_pairing_excludes_unavailable_drivers_and_vehicles(settings, shift):
    pairs = pair_drivers(settings, shift)["pairs"]

    assert "D01" not in {p["driver_id"] for p in pairs}
    assert "D14" not in {p["driver_id"] for p in pairs}
    assert "V05" not in {p["vehicle_id"] for p in pairs}


def test_a_failed_solver_gives_a_valid_backup_plan(settings, shift, assessments):
    def broken_solver(*args, **kwargs):
        raise RuntimeError("Solver is down")

    result = generate_plan(settings, shift, assessments, solver=broken_solver)

    assert result["method"] == "backup"
    assert check_plan(settings, shift, assessments, result["plan"])["valid"] is True
    assert set(plan_ids(result["plan"]).values()) == {1}


def test_a_solver_plan_with_a_hard_violation_gets_repaired(settings, shift, assessments):
    def overloading_solver(settings, stops, pairs, hub, plan_date):
        # Puts every chilled stop's neighbour on one bike, which breaks the bike's limits.
        pair = next(p for p in pairs if p["vehicle_id"] == "V02")
        return {pair["vehicle_id"]: [s for s in stops if s["allowed_types"]][:30]}

    result = generate_plan(settings, shift, assessments, solver=overloading_solver)

    assert check_plan(settings, shift, assessments, result["plan"])["valid"] is True
    assert set(plan_ids(result["plan"]).values()) == {1}
    assert any("V02" in e["reason"] or "over" in e["reason"] or "limit" in e["reason"] for e in result["plan"]["unassigned"])


def test_a_july_plan_has_no_riding_during_the_ban(settings, shift, assessments):
    july = copy.deepcopy(shift)
    july["plan_date"] = "2026-07-15"

    result = generate_plan(settings, july, assessments)

    checked = check_plan(settings, july, assessments, result["plan"])
    assert checked["valid"] is True
    assert "riding_ban" not in [v["rule"] for v in checked["violations"]]


def test_metrics_include_the_estimated_failure_cost(generated):
    failure = generated["failure"]

    assert failure["expected_failed"] > 0
    assert failure["cost"] == pytest.approx(failure["expected_failed"] * 12, abs=0.7)
    assert failure["cost_per_expected_delivered"] > 0


def test_the_export_keeps_every_required_field(settings, shift, assessments, generated):
    export = build_export(settings, shift, assessments, generated["plan"])

    assert {"plan_date", "client_id", "hub_id", "assumptions", "routes", "unassigned", "addresses"} <= set(export)
    assert export["client_id"] == "sahra-express"
    assert export["assumptions"]
    route = export["routes"][0]
    assert {"route_id", "driver_id", "vehicle_id", "depart_hub", "return_hub", "distance_km", "stops"} <= set(route)
    assert {"seq", "consignment_ids", "lat", "lng", "eta", "service_min"} <= set(route["stops"][0])
    for address in export["addresses"]:
        assert {"consignment_id", "lat", "lng", "confidence", "quality", "action"} <= set(address)


def test_the_export_accounts_for_every_row_in_its_addresses(shift, settings, assessments, generated):
    export = build_export(settings, shift, assessments, generated["plan"])
    by_id = {a["consignment_id"]: a for a in export["addresses"]}

    assert set(by_id) == {r["id"] for r in shift["consignments"]}
    assert by_id["C0334"]["quality"] == "INVALID"
    assert by_id["C0051"]["lat"] is None
    assert all(a["quality"] in ("GOOD", "AMBIGUOUS", "INCOMPLETE", "INVALID") for a in export["addresses"])


def test_the_export_uses_the_checkers_times(settings, shift, assessments, generated):
    export = build_export(settings, shift, assessments, generated["plan"])
    checked = check_plan(settings, shift, assessments, generated["plan"])

    assert export["routes"][0]["return_hub"] == checked["routes"][0]["return_hub"]
    assert export["routes"][0]["stops"][0]["eta"] == checked["routes"][0]["stops"][0]["eta"]
