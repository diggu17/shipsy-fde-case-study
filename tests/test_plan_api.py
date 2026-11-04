import pytest

from conftest import FIXTURES, SAMPLE_DATA, load_seed, riyadh_files, sample_files

CLIENT = "/clients/sahra-express"
SHIFT = f"{CLIENT}/hubs/AQ3/shifts/2026-10-14"
PLANS = f"{SHIFT}/plans"


def csv_file(path):
    return {"file": ("file.csv", path.read_bytes(), "text/csv")}


def multipart(files):
    return {name: (f"{name}.csv", data, "text/csv") for name, data in files.items()}


def messages(response):
    return [p["message"] for p in response.json()["detail"]["problems"]]


@pytest.fixture
def assessed(api, sahra):
    sahra["planning"]["solver_seconds"] = 2
    api.post("/clients", json=sahra)
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))
    api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv"))
    api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv"))
    api.post(f"{SHIFT}/addresses")


@pytest.fixture
def planned(api, assessed):
    response = api.post(PLANS)
    assert response.status_code == 201
    return response.json()


def test_generating_saves_a_valid_draft_with_its_settings(api, planned, db):
    assert planned["version"] == 1
    assert planned["status"] == "draft"
    assert planned["check"]["valid"] is True
    assert planned["method"] == "solver"
    assert planned["failure"]["expected_failed"] > 0
    saved = db.plans.find_one({"version": 1})
    assert saved["settings"]["money"]["late_penalty_per_parcel"] == 5


def test_generating_needs_assessed_addresses(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))

    response = api.post(PLANS)

    assert response.status_code == 404
    assert messages(response) == ["Addresses have not been assessed yet"]


def test_reading_with_no_plan_is_not_found(api, assessed):
    response = api.get(f"{PLANS}/latest")

    assert response.status_code == 404
    assert messages(response) == ["Plan not found"]


def test_changing_a_setting_later_does_not_change_a_saved_plan(api, planned, sahra):
    before = api.get(f"{PLANS}/latest").json()["check"]["metrics"]
    sahra["money"]["late_penalty_per_parcel"] = 500
    assert api.put(CLIENT, json=sahra).status_code == 200

    assert api.get(f"{PLANS}/latest").json()["check"]["metrics"] == before


def test_an_edit_is_checked_and_saved_as_a_new_draft(api, planned):
    plan = planned["plan"]
    plan["routes"][0]["driver_id"] = "D01"

    response = api.put(f"{PLANS}/latest", json=plan)

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["source"] == "edited"
    assert body["check"]["valid"] is False
    assert "driver_unavailable" in [v["rule"] for v in body["check"]["violations"]]


def test_the_manager_can_approve_over_a_limit_and_the_alert_is_kept(api, planned):
    plan = planned["plan"]
    plan["routes"][0]["driver_id"] = "D01"
    api.put(f"{PLANS}/latest", json=plan)

    response = api.post(f"{PLANS}/latest/approve")

    assert response.status_code == 200
    saved = api.get(f"{PLANS}/latest").json()
    assert saved["status"] == "approved"
    assert any("D01" in alert for alert in saved["approved_with_alerts"])
    assert "driver_unavailable" in [v["rule"] for v in saved["check"]["violations"]]
    assert any("D01" in alert for alert in api.get(f"{PLANS}/latest/export").json()["approved_with_alerts"])


def test_a_plan_with_no_alerts_exports_none(api, planned):
    api.post(f"{PLANS}/latest/approve")

    assert "approved_with_alerts" not in api.get(f"{PLANS}/latest/export").json()


def test_a_broken_plan_cant_be_approved(api, planned):
    plan = planned["plan"]
    plan["routes"][1]["stops"].append(dict(plan["routes"][0]["stops"][0], seq=99))
    api.put(f"{PLANS}/latest", json=plan)

    response = api.post(f"{PLANS}/latest/approve")

    assert response.status_code == 409
    assert api.get(f"{PLANS}/latest").json()["status"] == "draft"


# Moving a parcel


def preview(api, plan, consignment_id, route_id):
    return api.post(f"{PLANS}/latest/preview-move", json={"plan": plan, "consignment_id": consignment_id, "route_id": route_id})


def route_of(plan, consignment_id):
    return next((r for r in plan["routes"] for s in r["stops"] if consignment_id in s["consignment_ids"]), None)


def non_motorbike_routes(api, plan):
    vehicles = {v["id"]: v["values"]["type"] for v in api.get(SHIFT).json()["vehicles"]}
    return [r for r in plan["routes"] if vehicles[r["vehicle_id"]] != "MOTORBIKE"]


def test_a_move_can_apply_with_no_alert(api, planned):
    plan = planned["plan"]
    rows = {r["id"]: r["values"] for r in api.get(SHIFT).json()["consignments"]}
    any_time = [c for r in plan["routes"] for s in r["stops"] for c in s["consignment_ids"] if not rows[c]["slot_start"]]
    times = {r["route_id"]: r["working_minutes"] for r in planned["check"]["routes"]}
    targets = sorted(non_motorbike_routes(api, plan), key=lambda r: times[r["route_id"]])
    moves = ((c, t) for t in targets for c in any_time if route_of(plan, c)["route_id"] != t["route_id"])
    bodies = (preview(api, plan, c, t["route_id"]).json() for c, t in moves)

    body = next(b for b in bodies if not b["alerts"])

    assert body["check"]["valid"] is True
    assert body["after"]["parcels"] == body["before"]["parcels"] + 1


def test_going_over_a_limit_gives_an_alert_with_before_and_after_values(api, planned, db):
    db.plans.update_one({}, {"$set": {"settings.driver_rules.max_working_hours": 1}})
    plan = planned["plan"]
    target = max(non_motorbike_routes(api, plan), key=lambda r: len(r["stops"]))
    source = next(r for r in plan["routes"] if r is not target)

    body = preview(api, plan, source["stops"][0]["consignment_ids"][0], target["route_id"]).json()

    assert "working_time" in [a["rule"] for a in body["alerts"]]
    assert body["after"]["working_minutes"] > body["before"]["working_minutes"]
    assert body["after"]["total_cost"] > body["before"]["total_cost"]


def test_a_preview_saves_nothing(api, planned):
    plan = planned["plan"]
    source, target = plan["routes"][:2]

    preview(api, plan, source["stops"][0]["consignment_ids"][0], target["route_id"])

    saved = api.get(f"{PLANS}/latest").json()
    assert saved["version"] == 1
    assert saved["plan"] == plan


def test_a_parcel_goes_on_its_new_drivers_route_exactly_once(api, planned):
    plan = planned["plan"]
    source, target = plan["routes"][:2]
    parcel = source["stops"][0]["consignment_ids"][0]

    body = preview(api, plan, parcel, target["route_id"]).json()

    assert route_of(body["plan"], parcel)["route_id"] == target["route_id"]
    assert "consignment_count" not in [v["rule"] for v in body["check"]["violations"]]
    new_stops = next(r for r in body["plan"]["routes"] if r["route_id"] == target["route_id"])["stops"]
    assert [s["seq"] for s in new_stops] == list(range(1, len(new_stops) + 1))


def test_moving_one_parcel_leaves_the_others_at_its_stop(api, planned):
    plan = planned["plan"]
    source = next(r for r in plan["routes"] if any(len(s["consignment_ids"]) > 1 for s in r["stops"]))
    stop = next(s for s in source["stops"] if len(s["consignment_ids"]) > 1)
    moved, stayed = stop["consignment_ids"][:2]
    target = next(r for r in plan["routes"] if r is not source)

    body = preview(api, plan, moved, target["route_id"]).json()

    assert route_of(body["plan"], stayed)["route_id"] == source["route_id"]
    assert route_of(body["plan"], moved)["route_id"] == target["route_id"]


def test_moving_a_routes_only_parcel_removes_the_route(api, planned):
    plan = planned["plan"]
    source, target = plan["routes"][:2]
    plan["unassigned"] += [{"consignment_id": c, "reason": "x"} for s in source["stops"][1:] for c in s["consignment_ids"]]
    source["stops"] = source["stops"][:1]
    source["stops"][0]["consignment_ids"] = source["stops"][0]["consignment_ids"][:1]

    body = preview(api, plan, source["stops"][0]["consignment_ids"][0], target["route_id"]).json()

    assert source["route_id"] not in [r["route_id"] for r in body["plan"]["routes"]]


def test_a_parcel_can_be_taken_off_the_plan(api, planned):
    plan = planned["plan"]
    parcel = plan["routes"][0]["stops"][0]["consignment_ids"][0]

    body = preview(api, plan, parcel, None).json()

    assert route_of(body["plan"], parcel) is None
    assert {"consignment_id": parcel, "reason": "Moved off the route by the manager"} in body["plan"]["unassigned"]
    assert body["alerts"] == []


def test_an_unassigned_parcel_can_go_to_a_driver(api, planned):
    plan = planned["plan"]
    parcel = plan["routes"][0]["stops"][0]["consignment_ids"][0]
    off = preview(api, plan, parcel, None).json()["plan"]

    body = preview(api, off, parcel, off["routes"][-1]["route_id"]).json()

    assert parcel not in [e["consignment_id"] for e in body["plan"]["unassigned"]]
    assert route_of(body["plan"], parcel)["route_id"] == off["routes"][-1]["route_id"]


def test_a_parcel_with_no_location_cant_go_to_a_driver(api, planned):
    plan = planned["plan"]

    response = preview(api, plan, "C0051", plan["routes"][0]["route_id"])

    assert response.status_code == 422
    assert any("no location" in message for message in messages(response))


def test_moving_a_parcel_to_its_own_driver_is_refused(api, planned):
    plan = planned["plan"]
    route = plan["routes"][0]

    assert preview(api, plan, route["stops"][0]["consignment_ids"][0], route["route_id"]).status_code == 422


def test_a_valid_plan_can_be_approved_even_with_soft_violations(api, planned):
    assert api.post(f"{PLANS}/latest/approve").status_code == 200

    assert api.get(f"{PLANS}/latest").json()["status"] == "approved"


def test_an_edit_after_approval_needs_approving_again(api, planned):
    api.post(f"{PLANS}/latest/approve")

    api.put(f"{PLANS}/latest", json=planned["plan"])

    assert api.get(f"{PLANS}/latest").json()["status"] == "draft"


def test_the_export_has_the_required_format(api, planned):
    export = api.get(f"{PLANS}/latest/export").json()

    assert {"plan_date", "client_id", "hub_id", "assumptions", "routes", "unassigned", "addresses"} <= set(export)
    assert export["hub_id"] == "AQ3"


def test_replanning_returns_a_proposal_and_saves_nothing_until_accepted(api, planned):
    driver = next(r["driver_id"] for r in planned["plan"]["routes"] if r["depart_hub"] == "09:00")

    response = api.post(f"{PLANS}/replan", json={"driver_id": driver, "time": "08:00"})

    assert response.status_code == 200
    proposal = response.json()
    assert proposal["check"]["valid"] is True
    assert api.get(f"{PLANS}/latest").json()["version"] == 1

    accepted = api.put(f"{PLANS}/latest", json=proposal["plan"])
    assert accepted.json()["version"] == 2


def test_replanning_for_a_driver_with_no_route_is_rejected(api, planned):
    response = api.post(f"{PLANS}/replan", json={"driver_id": "NOBODY", "time": "08:00"})

    assert response.status_code == 422


def test_replanning_with_a_bad_time_is_rejected(api, planned):
    driver = planned["plan"]["routes"][0]["driver_id"]

    response = api.post(f"{PLANS}/replan", json={"driver_id": driver, "time": "morning"})

    assert response.status_code == 422


def test_clients_can_be_listed(api, planned):
    assert api.get("/clients").json() == [
        {"client_id": "sahra-express", "name": "Sahra Express", "hubs": ["AQ3"]}
    ]


# Part 7: the whole product for a second client, with no code changes.


def test_a_second_client_goes_through_the_whole_product_with_no_code_changes(api):
    riyadh = load_seed("demo-riyadh-courier")
    riyadh["planning"]["solver_seconds"] = 1
    assert api.post("/clients", json=riyadh).status_code == 201
    client = "/clients/demo-riyadh-courier"
    shift = f"{client}/hubs/RUH1/shifts/2026-11-20"
    assert api.post(f"{shift}/uploads", files=multipart(riyadh_files())).status_code == 201
    assert api.post(f"{client}/areas", files=csv_file(FIXTURES / "riyadh" / "areas.csv")).status_code == 200
    assert api.post(f"{client}/delivery-history", files=csv_file(FIXTURES / "riyadh" / "delivery_history.csv")).status_code == 200
    assert api.post(f"{shift}/addresses").status_code == 200

    planned = api.post(f"{shift}/plans")

    assert planned.status_code == 201
    body = planned.json()
    assert body["check"]["valid"] is True
    routed = [c for r in body["plan"]["routes"] for s in r["stops"] for c in s["consignment_ids"]]
    assert routed == ["R1001"]
    assert body["plan"]["routes"][0]["vehicle_id"] == "P1"
    assert body["plan"]["routes"][0]["driver_id"] == "S1"
    assert body["check"]["routes"][0]["cost"]["fixed"] == 70
    assert api.post(f"{shift}/plans/latest/approve").status_code == 200
    export = api.get(f"{shift}/plans/latest/export").json()
    assert export["client_id"] == "demo-riyadh-courier"
    assert export["hub_id"] == "RUH1"
