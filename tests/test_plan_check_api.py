import pytest

from conftest import FIXTURES, SAMPLE_DATA, load_seed, riyadh_files, sample_files

CLIENT = "/clients/sahra-express"
SHIFT = f"{CLIENT}/hubs/AQ3/shifts/2026-10-14"


def csv_file(path):
    return {"file": ("file.csv", path.read_bytes(), "text/csv")}


def multipart(files):
    return {name: (f"{name}.csv", data, "text/csv") for name, data in files.items()}


@pytest.fixture
def assessed(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))
    api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv"))
    api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv"))
    rows = api.post(f"{SHIFT}/addresses").json()["assessments"]
    return {row["consignment_id"]: row for row in rows}


def real_stop(assessments, seq, row_id):
    row = assessments[row_id]
    return {"seq": seq, "consignment_ids": [row_id], "lat": row["lat"] or 25.1, "lng": row["lng"] or 55.1}


def everything_else_unassigned(api, used):
    rows = api.get(SHIFT).json()["consignments"]
    return [
        {"consignment_id": row["id"], "reason": "Not in this test"}
        for row in rows
        if row["id"] not in used and row["result"] != "rejected"
    ]


def plan_of(routes, unassigned):
    return {"plan_date": "2026-10-14", "client_id": "sahra-express", "hub_id": "AQ3", "routes": routes, "unassigned": unassigned}


def test_a_broken_plan_on_the_real_sample_lists_every_expected_violation(api, assessed):
    used = {"C0177", "C0026", "C0100", "C0051", "C0104"}
    routes = [
        {"route_id": "R1", "driver_id": "D06", "vehicle_id": "V13", "depart_hub": "09:00",
         "stops": [real_stop(assessed, 1, "C0177"), real_stop(assessed, 2, "C0100")]},
        {"route_id": "R2", "driver_id": "D01", "vehicle_id": "V04", "depart_hub": "09:00",
         "stops": [real_stop(assessed, 1, "C0026"), real_stop(assessed, 2, "C0100")]},
        {"route_id": "R3", "driver_id": "D04", "vehicle_id": "V05", "depart_hub": "09:00",
         "stops": [real_stop(assessed, 1, "C0051"), real_stop(assessed, 2, "C0104")]},
    ]

    response = api.post(f"{SHIFT}/plans/check", json=plan_of(routes, everything_else_unassigned(api, used)))

    assert response.status_code == 200
    body = response.json()
    found = {(v["rule"], v["route_id"], v["consignment_id"]) for v in body["violations"] if v["severity"] == "hard"}
    assert body["valid"] is False
    assert ("licence", "R1", None) in found
    assert ("cash_limit", "R1", None) in found
    assert ("consignment_count", None, "C0100") in found
    assert ("driver_unavailable", "R2", None) in found
    assert ("not_plannable", "R2", "C0026") in found
    assert ("overweight", "R2", None) in found
    assert ("vehicle_unavailable", "R3", None) in found
    assert ("no_location", "R3", "C0051") in found


def test_real_address_text_sets_the_stop_type(api, assessed):
    used = {"C0105"}
    routes = [{"route_id": "R1", "driver_id": "D04", "vehicle_id": "V13", "depart_hub": "09:00",
               "stops": [real_stop(assessed, 1, "C0105")]}]

    body = api.post(f"{SHIFT}/plans/check", json=plan_of(routes, everything_else_unassigned(api, used))).json()

    # Villa 91, Street 34: a villa with no extras.
    assert body["valid"] is True
    assert body["routes"][0]["stops"][0]["stop_type_guessed"] is False
    assert body["routes"][0]["stops"][0]["service_min"] == 4


def test_checking_needs_assessed_addresses(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))

    response = api.post(f"{SHIFT}/plans/check", json=plan_of([], []))

    assert response.status_code == 404
    assert response.json()["detail"]["problems"][0]["message"] == "Addresses have not been assessed yet"


def test_a_plan_that_is_not_an_object_is_reported_without_crashing(api, assessed):
    response = api.post(f"{SHIFT}/plans/check", json=["not", "a", "plan"])

    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_the_second_client_checks_a_plan_with_no_code_changes(api):
    api.post("/clients", json=load_seed("demo-riyadh-courier"))
    client = "/clients/demo-riyadh-courier"
    shift = f"{client}/hubs/RUH1/shifts/2026-11-20"
    api.post(f"{shift}/uploads", files=multipart(riyadh_files()))
    api.post(f"{client}/areas", files=csv_file(FIXTURES / "riyadh" / "areas.csv"))
    api.post(f"{client}/delivery-history", files=csv_file(FIXTURES / "riyadh" / "delivery_history.csv"))
    rows = {a["consignment_id"]: a for a in api.post(f"{shift}/addresses").json()["assessments"]}
    plan = {
        "plan_date": "2026-11-20",
        "client_id": "demo-riyadh-courier",
        "hub_id": "RUH1",
        "routes": [
            {"route_id": "R1", "driver_id": "S1", "vehicle_id": "P1", "depart_hub": "08:00",
             "stops": [{"seq": 1, "consignment_ids": ["R1001"], "lat": rows["R1001"]["lat"], "lng": rows["R1001"]["lng"]}]}
        ],
        "unassigned": [{"consignment_id": "R1003", "reason": "Held"}],
    }

    body = api.post(f"{shift}/plans/check", json=plan).json()

    # Villa 5, COD 3, on a scooter costing SAR 70 a day.
    assert body["valid"] is True
    assert body["routes"][0]["stops"][0]["service_min"] == 8
    assert body["routes"][0]["cost"]["fixed"] == 70
