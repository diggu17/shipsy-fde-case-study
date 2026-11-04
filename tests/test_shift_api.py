import pytest

from conftest import load_seed, riyadh_files, sample_files

SHIFT = "/clients/sahra-express/hubs/AQ3/shifts/2026-10-14"


def multipart(files):
    return {name: (f"{name}.csv", data, "text/csv") for name, data in files.items()}


def row(shift, name, row_id):
    return next(r for r in shift[name] if r["id"] == row_id)


@pytest.fixture
def sahra_client(api, sahra):
    assert api.post("/clients", json=sahra).status_code == 201


@pytest.fixture
def uploaded(api, sahra_client):
    assert api.post(f"{SHIFT}/uploads", files=multipart(sample_files())).status_code == 201


def problem_messages(response):
    return [p["message"] for p in response.json()["detail"]["problems"]]


def test_uploading_the_sample_returns_a_report_that_accounts_for_every_row(api, sahra_client):
    response = api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["report"]["consignments"]["received"] == 432
    assert body["report"]["consignments"]["duplicates_removed"] == 3


def test_reading_the_upload_returns_its_rows(api, uploaded):
    shift = api.get(SHIFT).json()

    assert (shift["client_id"], shift["hub_id"], shift["plan_date"]) == ("sahra-express", "AQ3", "2026-10-14")
    assert row(shift, "consignments", "C0119")["result"] == "held"
    assert row(shift, "drivers", "D17")["values"]["shift_end"] == "18:00"


def test_a_missing_column_rejects_the_upload_and_saves_nothing(api, sahra_client):
    files = sample_files()
    files["consignments"] = files["consignments"].replace(b"weight_kg", b"mass")

    response = api.post(f"{SHIFT}/uploads", files=multipart(files))

    assert response.status_code == 422
    assert problem_messages(response) == ["Missing column: weight_kg"]
    assert api.get(SHIFT).status_code == 404


def test_an_invalid_plan_date_is_rejected(api, sahra_client):
    response = api.post(
        "/clients/sahra-express/hubs/AQ3/shifts/2026-13-40/uploads", files=multipart(sample_files())
    )

    assert response.status_code == 422
    assert problem_messages(response) == ["Plan date must be a real date in YYYY-MM-DD format"]


def test_an_unknown_client_is_rejected(api):
    response = api.post("/clients/nobody/hubs/AQ3/shifts/2026-10-14/uploads", files=multipart(sample_files()))

    assert response.status_code == 404
    assert problem_messages(response) == ["Client not found"]


def test_an_unknown_hub_is_rejected(api, sahra_client):
    response = api.post(
        "/clients/sahra-express/hubs/NOPE/shifts/2026-10-14/uploads", files=multipart(sample_files())
    )

    assert response.status_code == 404
    assert problem_messages(response) == ["Hub not found"]


def test_a_second_upload_is_used_and_the_first_is_kept(api, db, uploaded):
    files = sample_files()
    files["drivers"] = files["drivers"].replace(b"D04,Imtiaz Nair,LIGHT_VEHICLE,09:00,18:00,2,AVAILABLE", b"D04,Imtiaz Nair,LIGHT_VEHICLE,09:00,18:00,2,ON_LEAVE")

    response = api.post(f"{SHIFT}/uploads", files=multipart(files))

    assert response.json()["version"] == 2
    shift = api.get(SHIFT).json()
    assert shift["version"] == 2
    assert row(shift, "drivers", "D04")["plannable"] is False
    assert db.shift_uploads.count_documents({"client_id": "sahra-express"}) == 2


def test_a_shift_with_no_upload_is_not_found(api, sahra_client):
    response = api.get(SHIFT)

    assert response.status_code == 404
    assert problem_messages(response) == ["Shift upload not found"]


def resolve(api, name, row_id, **body):
    return api.post(f"{SHIFT}/{name}/{row_id}/resolve", json=body)


def test_accepting_a_suggestion_makes_the_row_plannable_and_keeps_the_original(api, uploaded):
    response = resolve(api, "consignments", "C0119", action="accept_suggestion")

    assert response.status_code == 200
    resolved = row(api.get(SHIFT).json(), "consignments", "C0119")
    assert resolved["values"]["weight_kg"] == 2.87
    assert resolved["plannable"] is True
    assert resolved["result"] != "held"
    assert resolved["original"]["weight_kg"] == "2870"
    assert resolved["decision"]["action"] == "accept_suggestion"


def test_entering_an_impossible_weight_is_rejected_and_the_row_stays_held(api, uploaded):
    response = resolve(api, "consignments", "C0119", action="enter_values", values={"weight_kg": -5})

    assert response.status_code == 422
    assert problem_messages(response) == ["Weight isn't possible: -5"]
    assert row(api.get(SHIFT).json(), "consignments", "C0119")["result"] == "held"


def test_entering_a_correct_size_makes_the_row_plannable_and_keeps_the_original(api, uploaded):
    response = resolve(
        api,
        "consignments",
        "C0303",
        action="enter_values",
        values={"length_cm": 60, "width_cm": 40, "height_cm": 40},
    )

    assert response.status_code == 200
    resolved = row(api.get(SHIFT).json(), "consignments", "C0303")
    assert resolved["values"]["length_cm"] == 60
    assert resolved["plannable"] is True
    assert resolved["original"]["length_cm"] == "600"


def test_leaving_a_row_out_keeps_it_out_of_the_plan(api, uploaded):
    response = resolve(api, "consignments", "C0026", action="leave_out")

    assert response.status_code == 200
    resolved = row(api.get(SHIFT).json(), "consignments", "C0026")
    assert resolved["plannable"] is False
    assert resolved["decision"] == {"action": "leave_out"}


def test_resolving_updates_the_report(api, uploaded):
    before = api.get(SHIFT).json()["report"]["consignments"]["held"]

    resolve(api, "consignments", "C0119", action="accept_suggestion")

    assert api.get(SHIFT).json()["report"]["consignments"]["held"] == before - 1


def test_a_row_that_is_not_held_cannot_be_resolved(api, uploaded):
    response = resolve(api, "consignments", "C0367", action="leave_out")

    assert response.status_code == 409
    assert problem_messages(response) == ["This row isn't held"]


def test_a_row_that_does_not_exist_cannot_be_resolved(api, uploaded):
    response = resolve(api, "consignments", "C9999", action="leave_out")

    assert response.status_code == 404
    assert problem_messages(response) == ["Row not found"]


def test_an_id_cannot_be_changed_by_resolving(api, uploaded):
    response = resolve(api, "consignments", "C0119", action="enter_values", values={"consignment_id": "C1"})

    assert response.status_code == 422
    assert problem_messages(response) == ["Can't change: consignment_id"]


def test_resolving_keeps_a_duplicate_finding(api, uploaded):
    # C0100 is the conflicting duplicate, so a held copy of it must keep that history.
    files = sample_files()
    files["consignments"] = files["consignments"].replace(b",4.33,", b",-1,")
    api.post(f"{SHIFT}/uploads", files=multipart(files))

    response = resolve(api, "consignments", "C0100", action="enter_values", values={"weight_kg": 4.33})

    assert response.status_code == 200
    assert "duplicate_conflict" in [f["code"] for f in response.json()["findings"]]


def test_the_second_client_uploads_its_own_format_with_no_code_changes(api):
    assert api.post("/clients", json=load_seed("demo-riyadh-courier")).status_code == 201
    path = "/clients/demo-riyadh-courier/hubs/RUH1/shifts/2026-11-20"

    assert api.post(f"{path}/uploads", files=multipart(riyadh_files())).status_code == 201

    shift = api.get(path).json()
    first = row(shift, "consignments", "R1001")
    assert first["result"] == "accepted"
    assert first["findings"] == []
    assert first["plannable"] is True
    assert first["original"]["payment_type"] == "Cash"
    assert first["values"]["payment_type"] == "COD"
    assert first["values"]["cod_amount"] == 120
    assert first["values"]["handling"] == "NONE"
    assert row(shift, "consignments", "R1002")["result"] == "rejected"
    assert row(shift, "consignments", "R1003")["suggestion"] == {"weight_kg": "0.9"}
    assert row(shift, "drivers", "S1")["values"]["licence"] == "RIDER"
    assert row(shift, "drivers", "S2")["plannable"] is False
    assert row(shift, "vehicles", "P1")["values"]["type"] == "SCOOTER"
    assert row(shift, "vehicles", "P1")["values"]["max_weight_kg"] == 30
    assert row(shift, "vehicles", "P2")["plannable"] is False
