import pytest

from conftest import FIXTURES, SAMPLE_DATA, load_seed, riyadh_files, sample_files

CLIENT = "/clients/sahra-express"
SHIFT = f"{CLIENT}/hubs/AQ3/shifts/2026-10-14"
ADDRESSES = f"{SHIFT}/addresses"


def csv_file(path):
    return {"file": ("file.csv", path.read_bytes(), "text/csv")}


def multipart(files):
    return {name: (f"{name}.csv", data, "text/csv") for name, data in files.items()}


def problem_messages(response):
    return [p["message"] for p in response.json()["detail"]["problems"]]


@pytest.fixture
def ready(api, sahra):
    assert api.post("/clients", json=sahra).status_code == 201
    assert api.post(f"{SHIFT}/uploads", files=multipart(sample_files())).status_code == 201
    assert api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv")).status_code == 200
    assert api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv")).status_code == 200


@pytest.fixture
def assessed(api, ready):
    assert api.post(ADDRESSES).status_code == 200


def read_row(api, row_id):
    return next(a for a in api.get(ADDRESSES).json()["assessments"] if a["consignment_id"] == row_id)


def test_uploading_history_reports_what_was_cleaned(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv"))

    response = api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv"))

    assert response.status_code == 200
    assert response.json()["swapped"] == 56
    assert response.json()["kept"] == 5408


def test_uploading_history_again_replaces_it(api, db, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv"))
    for _ in range(2):
        api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv"))

    assert db.delivery_history.count_documents({"client_id": "sahra-express", "location_unreliable": {"$ne": True}}) == 5408


def test_history_needs_an_area_list_first(api, sahra):
    api.post("/clients", json=sahra)

    response = api.post(f"{CLIENT}/delivery-history", files=csv_file(SAMPLE_DATA / "delivery_history.csv"))

    assert response.status_code == 409
    assert problem_messages(response) == ["Upload the client's area list first"]


def test_an_area_file_with_a_missing_column_is_rejected(api, sahra):
    api.post("/clients", json=sahra)

    response = api.post(
        f"{CLIENT}/areas", files={"file": ("areas.csv", b"area_name,emirate\nDeira,Dubai\n", "text/csv")}
    )

    assert response.status_code == 422
    assert "Missing column: centroid_lat" in problem_messages(response)


def test_assessing_needs_an_area_list(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))

    response = api.post(ADDRESSES)

    assert response.status_code == 409
    assert problem_messages(response) == ["Upload the client's area list first"]


def test_assessing_needs_an_uploaded_shift(api, sahra):
    api.post("/clients", json=sahra)
    api.post(f"{CLIENT}/areas", files=csv_file(SAMPLE_DATA / "areas.csv"))

    response = api.post(ADDRESSES)

    assert response.status_code == 404
    assert problem_messages(response) == ["Shift upload not found"]


def test_assessing_saves_every_row_and_a_summary(api, ready):
    response = api.post(ADDRESSES)

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["assessed"] == 428
    assert sum(summary["by_quality"].values()) == 428
    assert len(api.get(ADDRESSES).json()["assessments"]) == 428
    assert read_row(api, "C0051")["quality"] == "INVALID"


def test_reading_before_assessing_is_not_found(api, ready):
    response = api.get(ADDRESSES)

    assert response.status_code == 404
    assert problem_messages(response) == ["Addresses have not been assessed yet"]


def test_a_new_upload_needs_assessing_again(api, assessed):
    api.post(f"{SHIFT}/uploads", files=multipart(sample_files()))

    assert api.get(ADDRESSES).status_code == 404


def test_confirming_a_location_updates_the_assessment(api, assessed):
    response = api.post(f"{ADDRESSES}/C0051/confirm", json={"lat": 25.2, "lng": 55.3})

    assert response.status_code == 200
    row = read_row(api, "C0051")
    assert (row["quality"], row["confidence"], row["routable"]) == ("GOOD", 1.0, True)


def test_assessing_again_keeps_confirmed_locations(api, assessed):
    api.post(f"{ADDRESSES}/C0051/confirm", json={"lat": 25.2, "lng": 55.3})

    api.post(ADDRESSES)

    assert read_row(api, "C0051")["source"] == "confirmed"


@pytest.mark.parametrize("body", [{"lat": 125, "lng": 55.3}, {"lat": 25.2, "lng": 255}, {"lat": "x", "lng": 55.3}])
def test_confirming_an_invalid_location_is_rejected(api, assessed, body):
    response = api.post(f"{ADDRESSES}/C0051/confirm", json=body)

    assert response.status_code == 422
    assert read_row(api, "C0051")["quality"] == "INVALID"


def test_confirming_an_unknown_row_is_not_found(api, assessed):
    response = api.post(f"{ADDRESSES}/C9999/confirm", json={"lat": 25.2, "lng": 55.3})

    assert response.status_code == 404
    assert problem_messages(response) == ["Row not found"]


def test_scoring_reports_accuracy_and_changes_nothing(api, assessed):
    before = api.get(ADDRESSES).json()

    response = api.post(f"{ADDRESSES}/accuracy", files=csv_file(SAMPLE_DATA / "ground_truth_sample.csv"))

    assert response.status_code == 200
    body = response.json()
    assert body["overall"]["verified"] == 50
    assert body["overall"]["median_m"] < body["area_centre_baseline"]["median_m"]
    assert api.get(ADDRESSES).json() == before


def test_the_verified_file_is_never_saved(api, db, assessed):
    collections_before = set(db.list_collection_names())

    api.post(f"{ADDRESSES}/accuracy", files=csv_file(SAMPLE_DATA / "ground_truth_sample.csv"))

    assert set(db.list_collection_names()) == collections_before


def test_the_second_client_runs_through_with_no_code_changes(api):
    api.post("/clients", json=load_seed("demo-riyadh-courier"))
    client = "/clients/demo-riyadh-courier"
    shift = f"{client}/hubs/RUH1/shifts/2026-11-20"
    api.post(f"{shift}/uploads", files=multipart(riyadh_files()))
    assert api.post(f"{client}/areas", files=csv_file(FIXTURES / "riyadh" / "areas.csv")).status_code == 200

    report = api.post(f"{client}/delivery-history", files=csv_file(FIXTURES / "riyadh" / "delivery_history.csv"))
    assert report.json()["unreliable_removed"] == 1

    result = api.post(f"{shift}/addresses").json()
    rows = {a["consignment_id"]: a for a in result["assessments"]}
    assert rows["R1001"]["quality"] == "GOOD"
    assert rows["R1001"]["confidence"] == 0.8
    assert rows["R1003"]["quality"] == "INCOMPLETE"
    assert rows["R1003"]["confidence"] == 0.15
    assert "R1002" not in rows


def test_recording_the_customers_area_makes_a_parcel_routable(api, assessed):
    response = api.post(f"{ADDRESSES}/C0062/customer-area", json={"area_name": "Al Barsha 1"})

    assert response.status_code == 200
    row = response.json()
    assert row["routable"] and row["area"] == "Al Barsha 1"
    assert row["verification"]["status"] == "answered"

    again = api.post(ADDRESSES).json()["assessments"]
    assert next(a for a in again if a["consignment_id"] == "C0062")["area"] == "Al Barsha 1"


def test_an_unknown_customer_area_is_rejected(api, assessed):
    response = api.post(f"{ADDRESSES}/C0062/customer-area", json={"area_name": "Atlantis"})

    assert response.status_code == 422


def test_the_summary_counts_geocoding_and_whatsapp(api, ready):
    summary = api.post(ADDRESSES).json()["summary"]

    assert summary["by_verification"]["whatsapp_triggered"] > 0
    assert summary["by_geocoding"]["skipped_incomplete_text"] > 0
