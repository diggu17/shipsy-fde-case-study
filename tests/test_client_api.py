from conftest import load_seed


def problem_messages(response):
    return [p["message"] for p in response.json()["detail"]["problems"]]


def test_sahras_settings_are_saved_with_the_values_from_the_brief(api, sahra):
    assert api.post("/clients", json=sahra).status_code == 201

    saved = api.get("/clients/sahra-express").json()

    assert saved["hubs"][0]["hub_id"] == "AQ3"
    assert (saved["hubs"][0]["lat"], saved["hubs"][0]["lng"]) == (25.1290, 55.2405)
    assert saved["day"] == {"plan_deadline": "08:30", "delivery_start": "09:00", "delivery_end": "21:00"}
    assert [(w["start"], w["end"]) for w in saved["slots"]["windows"]] == [
        ("09:00", "13:00"),
        ("13:00", "17:00"),
        ("17:00", "21:00"),
    ]
    assert saved["slots"]["any_time_offered"] is True
    assert {
        v["type_id"]: (v["max_weight_kg"], v["max_volume_m3"], v["fixed_cost_per_day"], v["cost_per_km"])
        for v in saved["vehicle_types"]
    } == {
        "MOTORBIKE": (25, 0.10, 90, 0.25),
        "SEDAN": (150, 0.40, 140, 0.45),
        "SMALL_VAN": (600, 3.0, 220, 0.70),
        "LARGE_VAN": (1200, 8.0, 320, 0.95),
        "CHILLED_VAN": (500, 2.5, 380, 1.10),
    }
    types = {v["type_id"]: v for v in saved["vehicle_types"]}
    assert (types["MOTORBIKE"]["max_item_weight_kg"], types["MOTORBIKE"]["max_item_length_cm"]) == (10, 60)
    assert types["LARGE_VAN"]["has_helper"] is True
    assert types["LARGE_VAN"]["required_for_items_over_kg"] == 30
    assert types["CHILLED_VAN"]["refrigerated"] is True
    assert saved["driver_rules"] == {
        "max_working_hours": 10,
        "break_minutes": 30,
        "break_required_over_hours": 6,
        "break_after_hours": 4,
        "overtime_rate_per_hour": 40,
        "cash_limit": 5000,
    }
    assert saved["money"] == {
        "revenue_per_delivery": 16,
        "failed_attempt_cost": 12,
        "late_penalty_per_parcel": 5,
    }
    assert saved["travel"]["speed_offpeak_kmh"] == 30
    assert saved["travel"]["speed_peak_kmh"] == 20
    assert saved["travel"]["vehicle_speed_factors"] == {"MOTORBIKE": 1.2}
    assert saved["travel"]["road_distance_factor"] == 1.4
    assert saved["stop_time"]["minutes_by_stop_type"] == {"villa": 4, "apartment": 8, "office_or_mall": 10}
    assert (
        saved["stop_time"]["cod_extra_minutes"],
        saved["stop_time"]["extra_parcel_minutes"],
        saved["stop_time"]["gate_extra_minutes"],
    ) == (2, 1, 5)


def test_a_document_with_mistakes_lists_them_all_and_saves_nothing(api, sahra):
    sahra["vehicle_types"][2]["max_weight_kg"] = -600
    sahra["hubs"][0]["lat"] = 125

    response = api.post("/clients", json=sahra)

    assert response.status_code == 422
    assert sorted(problem_messages(response)) == [
        "Hub location isn't a valid coordinate",
        "Weight limit must be above zero",
    ]
    assert api.get("/clients/sahra-express").status_code == 404


def test_creating_an_existing_client_is_rejected(api, sahra):
    api.post("/clients", json=sahra)

    response = api.post("/clients", json=sahra)

    assert response.status_code == 409
    assert problem_messages(response) == ["Client already exists"]


def test_a_valid_update_replaces_the_settings(api, sahra):
    api.post("/clients", json=sahra)
    sahra["money"]["late_penalty_per_parcel"] = 7

    assert api.put("/clients/sahra-express", json=sahra).status_code == 200

    assert api.get("/clients/sahra-express").json()["money"]["late_penalty_per_parcel"] == 7


def test_an_invalid_update_leaves_the_old_settings_in_place(api, sahra):
    api.post("/clients", json=sahra)
    sahra["vehicle_types"][2]["max_weight_kg"] = -600

    response = api.put("/clients/sahra-express", json=sahra)

    assert response.status_code == 422
    assert problem_messages(response) == ["Weight limit must be above zero"]
    saved = api.get("/clients/sahra-express").json()
    assert saved["vehicle_types"][2]["max_weight_kg"] == 600


def test_an_update_cannot_change_the_client_id(api, sahra):
    api.post("/clients", json=sahra)
    sahra["client_id"] = "someone-else"

    response = api.put("/clients/sahra-express", json=sahra)

    assert response.status_code == 422
    assert problem_messages(response) == ["Client ID must match the client being updated"]
    assert api.get("/clients/someone-else").status_code == 404


def test_updating_a_client_that_does_not_exist_is_rejected(api, sahra):
    response = api.put("/clients/sahra-express", json=sahra)

    assert response.status_code == 404
    assert problem_messages(response) == ["Client not found"]


def test_reading_a_client_that_does_not_exist_is_rejected(api):
    response = api.get("/clients/nobody")

    assert response.status_code == 404
    assert problem_messages(response) == ["Client not found"]


def test_reading_a_document_broken_by_a_direct_mongo_edit_lists_the_problems(api, db, sahra):
    api.post("/clients", json=sahra)
    db.clients.update_one({"_id": "sahra-express"}, {"$set": {"money.late_penalty_per_parcel": -5}})

    response = api.get("/clients/sahra-express")

    assert response.status_code == 409
    assert problem_messages(response) == ["Late penalty must not be negative"]


def test_a_second_client_with_different_settings_needs_no_code_changes(api, sahra):
    riyadh = load_seed("demo-riyadh-courier")

    assert api.post("/clients", json=sahra).status_code == 201
    assert api.post("/clients", json=riyadh).status_code == 201

    saved = api.get("/clients/demo-riyadh-courier").json()
    assert saved["currency"] == "SAR"
    assert [h["hub_id"] for h in saved["hubs"]] == ["RUH1", "RUH2"]
    assert [v["type_id"] for v in saved["vehicle_types"]] == ["SCOOTER", "PICKUP"]
    assert api.get("/clients/sahra-express").json()["currency"] == "AED"
