import copy

import pytest

from conftest import SAMPLE_DATA, load_seed, sample_files
from dispatch_planner.address import assess_shift, clean_history, load_areas
from dispatch_planner.plan_check import check_plan
from dispatch_planner.planner import generate_plan
from dispatch_planner.replan import replan_for_absent_driver
from dispatch_planner.upload import process_shift


def read(name):
    return (SAMPLE_DATA / f"{name}.csv").read_bytes()


@pytest.fixture(scope="module")
def world():
    settings = load_seed("sahra-express")
    settings["planning"]["solver_seconds"] = 5
    shift = process_shift(settings, sample_files())
    shift.update(client_id="sahra-express", hub_id="AQ3", plan_date="2026-10-14")
    areas = load_areas(settings, read("areas"))
    events, _ = clean_history(settings, areas, read("delivery_history"))
    assessments = assess_shift(settings, shift["consignments"], areas, events)
    result = generate_plan(settings, shift, assessments)
    return settings, shift, assessments, result


def route_for(plan, driver_id):
    return next(r for r in plan["routes"] if r["driver_id"] == driver_id)


def ids_of(route):
    return [c for stop in route["stops"] for c in stop["consignment_ids"]]


def every_id(plan):
    ids = [c for route in plan["routes"] for c in ids_of(route)]
    return ids + [e["consignment_id"] for e in plan["unassigned"]]


def morning_driver(plan):
    return next(r["driver_id"] for r in plan["routes"] if r["depart_hub"] == "09:00" and len(r["stops"]) >= 8)


def test_before_dispatch_the_absent_drivers_route_is_covered_and_the_plan_stays_valid(world):
    settings, shift, assessments, result = world
    driver = morning_driver(result["plan"])

    proposal = replan_for_absent_driver(settings, shift, assessments, result["plan"], driver, "08:00", unused_drivers=result["unused_drivers"])

    new_plan = proposal["plan"]
    assert driver not in {r["driver_id"] for r in new_plan["routes"]}
    assert check_plan(settings, shift, assessments, new_plan)["valid"] is True
    assert sorted(every_id(new_plan)) == sorted(every_id(result["plan"]))


def test_after_dispatch_completed_stops_stay_with_the_route(world):
    settings, shift, assessments, result = world
    plan = result["plan"]
    driver = morning_driver(plan)
    check = check_plan(settings, shift, assessments, plan)
    route = next(r for r in check["routes"] if r["driver_id"] == driver)
    # Part-way through the route: after the third stop has finished.
    now = route["stops"][3]["eta"]
    done = [c for stop in route["stops"][:3] for c in stop["consignment_ids"]]

    proposal = replan_for_absent_driver(settings, shift, assessments, plan, driver, now, unused_drivers=result["unused_drivers"])

    kept = route_for(proposal["plan"], driver)
    assert ids_of(kept)[: len(done)] == done
    assert set(ids_of(kept)) <= set(done)
    assert proposal["completed"] == done


def test_after_dispatch_routes_already_on_the_road_are_unchanged(world):
    settings, shift, assessments, result = world
    plan = result["plan"]
    driver = morning_driver(plan)
    check = check_plan(settings, shift, assessments, plan)
    route = next(r for r in check["routes"] if r["driver_id"] == driver)
    now = route["stops"][3]["eta"]
    from dispatch_planner.settings import time_to_minutes

    on_road = [r for r in plan["routes"] if r["driver_id"] != driver and time_to_minutes(r["depart_hub"]) <= time_to_minutes(now)]

    proposal = replan_for_absent_driver(settings, shift, assessments, plan, driver, now, unused_drivers=result["unused_drivers"])

    for original in on_road:
        assert route_for(proposal["plan"], original["driver_id"])["stops"] == original["stops"]


def test_the_plan_stays_valid_and_accounts_for_every_parcel_after_dispatch(world):
    settings, shift, assessments, result = world
    plan = result["plan"]
    driver = morning_driver(plan)
    check = check_plan(settings, shift, assessments, plan)
    now = next(r for r in check["routes"] if r["driver_id"] == driver)["stops"][3]["eta"]

    proposal = replan_for_absent_driver(settings, shift, assessments, plan, driver, now, unused_drivers=result["unused_drivers"])

    assert check_plan(settings, shift, assessments, proposal["plan"])["valid"] is True
    assert sorted(every_id(proposal["plan"])) == sorted(every_id(plan))


def test_a_proposal_does_not_change_the_original_plan(world):
    settings, shift, assessments, result = world
    before = copy.deepcopy(result["plan"])

    replan_for_absent_driver(settings, shift, assessments, result["plan"], morning_driver(before), "08:00", unused_drivers=result["unused_drivers"])

    assert result["plan"] == before


def test_a_proposal_lists_what_changed_and_the_cost_difference(world):
    settings, shift, assessments, result = world

    proposal = replan_for_absent_driver(settings, shift, assessments, result["plan"], morning_driver(result["plan"]), "08:00", unused_drivers=result["unused_drivers"])

    assert proposal["changes"]
    assert "cost_change" in proposal
    assert {"before", "after"} <= set(proposal["routed"])


def test_a_driver_with_no_route_is_rejected(world):
    settings, shift, assessments, result = world

    with pytest.raises(ValueError):
        replan_for_absent_driver(settings, shift, assessments, result["plan"], "NOBODY", "08:00", unused_drivers=[])
