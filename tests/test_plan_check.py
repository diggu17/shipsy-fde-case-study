import csv
import io

import pytest

from conftest import load_seed
from dispatch_planner.plan_check import check_plan
from dispatch_planner.upload import process_shift

HUB = (25.1290, 55.2405)
NEAR_HUB = (25.1390, 55.2505)
TEN_KM_NORTH = (25.218932, 55.2405)
DEFAULT_DRIVERS = [
    ("D1", "LIGHT_VEHICLE", "09:00", "18:00", "AVAILABLE"),
    ("M1", "MOTORCYCLE", "09:00", "18:00", "AVAILABLE"),
    ("L1", "LIGHT_VEHICLE", "12:00", "21:00", "AVAILABLE"),
    ("A1", "LIGHT_VEHICLE", "09:00", "18:00", "ON_LEAVE"),
    ("D2", "LIGHT_VEHICLE", "09:00", "18:00", "AVAILABLE"),
]
DEFAULT_VEHICLES = [
    ("VAN", "SMALL_VAN", "AVAILABLE"),
    ("VAN2", "SMALL_VAN", "AVAILABLE"),
    ("BIG", "LARGE_VAN", "AVAILABLE"),
    ("CAR", "SEDAN", "AVAILABLE"),
    ("BIKE", "MOTORBIKE", "AVAILABLE"),
    ("COLD", "CHILLED_VAN", "AVAILABLE"),
    ("OFF", "SMALL_VAN", "IN_MAINTENANCE"),
]


def to_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def parcel(row_id, location=NEAR_HUB, quality="GOOD", **fields):
    return {
        "id": row_id,
        "location": location,
        "quality": quality,
        "row": {
            "consignment_id": row_id,
            "shop_id": "S",
            "customer_phone": "+97150***0001",
            "address_line_1": "Flat 1",
            "address_line_2": "Tower",
            "area": "Deira",
            "city": "Dubai",
            "landmark": "",
            "weight_kg": 1,
            "length_cm": 20,
            "width_cm": 20,
            "height_cm": 20,
            "pieces": 1,
            "slot_start": "",
            "slot_end": "",
            "payment_type": "PREPAID",
            "cod_amount_aed": "",
            "handling": "NONE",
            "attempt_number": 1,
            "notes": "",
            **fields,
        },
    }


def world(parcels, drivers=DEFAULT_DRIVERS, vehicles=DEFAULT_VEHICLES, settings=None):
    settings = settings or load_seed("sahra-express")
    files = {
        "consignments": to_csv([p["row"] for p in parcels]),
        "drivers": to_csv(
            [
                {
                    "driver_id": d[0], "name": d[0], "licence_type": d[1], "shift_start": d[2],
                    "shift_end": d[3], "experience_months": 12, "status": d[4],
                }
                for d in drivers
            ]
        ),
        "vehicles": to_csv(
            [
                {
                    "vehicle_id": v[0], "type": v[1], "max_weight_kg": "", "max_volume_m3": "",
                    "refrigerated": "", "fixed_cost_aed_day": "", "cost_per_km_aed": "", "status": v[2],
                }
                for v in vehicles
            ]
        ),
    }
    shift = process_shift(settings, files)
    shift.update(client_id=settings["client_id"], hub_id="AQ3", plan_date="2026-10-14")
    assessments = [
        {
            "consignment_id": p["id"],
            "lat": p["location"][0] if p["location"] else None,
            "lng": p["location"][1] if p["location"] else None,
            "routable": p["location"] is not None,
            "quality": p["quality"],
            "area": p["row"]["area"],
            "unit": p["row"]["address_line_1"].casefold(),
        }
        for p in parcels
    ]
    return settings, shift, assessments


def stop(seq, ids, location=NEAR_HUB):
    return {"seq": seq, "consignment_ids": ids, "lat": location[0], "lng": location[1]}


def route(route_id, driver, vehicle, stops, depart="09:00"):
    return {"route_id": route_id, "driver_id": driver, "vehicle_id": vehicle, "depart_hub": depart, "stops": stops}


def plan(routes, unassigned=(), plan_date="2026-10-14"):
    return {
        "plan_date": plan_date,
        "client_id": "sahra-express",
        "hub_id": "AQ3",
        "routes": routes,
        "unassigned": [{"consignment_id": i, "reason": "Test"} for i in unassigned],
    }


def check(parcels, the_plan, **kwargs):
    settings, shift, assessments = world(parcels, **kwargs)
    return check_plan(settings, shift, assessments, the_plan)


def rules(result, severity=None):
    return [v["rule"] for v in result["violations"] if severity in (None, v["severity"])]


def one_parcel_plan(**fields):
    location = fields.pop("location", NEAR_HUB)
    vehicle = fields.pop("vehicle", "VAN")
    driver = fields.pop("driver", "D1")
    depart = fields.pop("depart", "09:00")
    parcels = [parcel("C1", location, **fields)]
    return parcels, plan([route("R1", driver, vehicle, [stop(1, ["C1"], location)], depart)])


# A valid plan


def test_a_valid_plan_has_no_violations():
    result = check(*one_parcel_plan())

    assert result["valid"] is True
    assert result["violations"] == []


# Times


def test_arrival_uses_road_distance_and_peak_then_off_peak_speed():
    result = check(*one_parcel_plan(location=TEN_KM_NORTH))

    first = result["routes"][0]["stops"][0]
    assert first["eta"] == "09:38"
    assert first["service_min"] == 8
    assert result["routes"][0]["return_hub"] == "10:14"
    assert result["routes"][0]["distance_km"] == pytest.approx(28.0, abs=0.05)


def test_a_motorbike_arrives_sooner():
    result = check(*one_parcel_plan(location=TEN_KM_NORTH, vehicle="BIKE", driver="M1"))

    assert result["routes"][0]["stops"][0]["eta"] < "09:38"


def test_an_early_arrival_waits_for_the_slot_and_counts_as_work():
    result = check(*one_parcel_plan(slot_start="13:00", slot_end="17:00"))

    first = result["routes"][0]["stops"][0]
    assert first["eta"] < "09:30"
    assert first["delivery_start"] == "13:00"
    assert "late" not in rules(result)


def test_service_time_adds_cod_pieces_and_gate_extras():
    result = check(
        *one_parcel_plan(
            address_line_1="Villa 3",
            payment_type="COD",
            cod_amount_aed=100,
            pieces=3,
            notes="Gate pass needed",
        )
    )

    # Villa 4, COD 2, two extra pieces 2, gate 5.
    assert result["routes"][0]["stops"][0]["service_min"] == 13


def test_a_gated_area_adds_gate_time():
    result = check(*one_parcel_plan(address_line_1="Villa 3", area="The Springs"))

    assert result["routes"][0]["stops"][0]["service_min"] == 9


def test_cod_time_is_added_for_each_cod_consignment_at_a_stop():
    parcels = [
        parcel("C1", payment_type="COD", cod_amount_aed=100),
        parcel("C2", payment_type="COD", cod_amount_aed=100),
    ]

    result = check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C2"])])]))

    # Apartment 8, two COD at 2 each, one extra parcel 1.
    assert result["routes"][0]["stops"][0]["service_min"] == 13


def test_an_office_uses_the_longest_stop_type_at_a_shared_stop():
    parcels = [parcel("C1", address_line_1="Villa 3"), parcel("C2", address_line_1="Office 3")]

    result = check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C2"])])]))

    # Office 10, one extra parcel 1.
    assert result["routes"][0]["stops"][0]["service_min"] == 11


def test_an_unknown_stop_type_uses_the_default_and_is_marked_as_a_guess():
    result = check(*one_parcel_plan(address_line_1="#813"))

    first = result["routes"][0]["stops"][0]
    assert first["service_min"] == 8
    assert first["stop_type_guessed"] is True
    assert result["metrics"]["guessed_stop_types"] == 1


def test_a_long_route_includes_a_break_after_four_hours_of_work():
    parcels = [
        parcel("C1", slot_start="13:00", slot_end="17:00"),
        parcel("C2"),
        parcel("C3", slot_start="17:00", slot_end="21:00"),
    ]
    stops = [stop(1, ["C1"]), stop(2, ["C2"]), stop(3, ["C3"])]

    result = check(parcels, plan([route("R1", "D1", "VAN", stops)]))

    # C1 begins at 13:00 and finishes at 13:08, four hours into a shift that lasts over six.
    the_route = result["routes"][0]
    assert the_route["break_after_seq"] == 1
    assert the_route["stops"][1]["eta"] == "13:38"


def test_a_short_route_has_no_break():
    assert check(*one_parcel_plan())["routes"][0]["break_after_seq"] is None


def test_wrong_times_in_a_plan_are_ignored():
    parcels, the_plan = one_parcel_plan(location=TEN_KM_NORTH)
    the_plan["routes"][0]["stops"][0]["eta"] = "20:00"
    the_plan["routes"][0]["return_hub"] = "23:00"
    the_plan["routes"][0]["distance_km"] = 999

    result = check(parcels, the_plan)

    assert result["routes"][0]["stops"][0]["eta"] == "09:38"
    assert result["routes"][0]["distance_km"] == pytest.approx(28.0, abs=0.05)


# Hard rules


@pytest.mark.parametrize("broken", [{}, {"routes": "none", "plan_date": "2026-10-14", "client_id": "sahra-express", "hub_id": "AQ3", "unassigned": []}])
def test_a_badly_formed_plan_is_reported_without_crashing(broken):
    result = check([parcel("C1")], broken)

    assert result["valid"] is False
    assert "plan_format" in rules(result)


def test_a_route_with_missing_fields_is_reported_without_crashing():
    result = check([parcel("C1")], plan([{"route_id": "R1"}], ["C1"]))

    assert "plan_format" in rules(result)


def test_a_plan_for_a_different_date_is_reported():
    result = check(*[[parcel("C1")], plan([], ["C1"], plan_date="2026-10-15")])

    assert "plan_format" in rules(result)


def test_a_consignment_on_two_routes_is_reported():
    parcels = [parcel("C1")]
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"])]), route("R2", "D2", "VAN2", [stop(1, ["C1"])])])

    assert "consignment_count" in rules(check(parcels, the_plan), "hard")


def test_a_consignment_both_routed_and_unassigned_is_reported():
    parcels = [parcel("C1")]

    assert "consignment_count" in rules(check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1"])])], ["C1"])), "hard")


def test_a_plannable_consignment_missing_from_the_plan_is_reported():
    parcels = [parcel("C1"), parcel("C2")]

    result = check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1"])])]))

    missing = [v for v in result["violations"] if v["rule"] == "consignment_count"]
    assert [v["consignment_id"] for v in missing] == ["C2"]


def test_a_consignment_from_another_shift_is_reported():
    parcels = [parcel("C1")]

    result = check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C9"])])]))

    assert "unknown_consignment" in rules(result, "hard")


def test_a_held_consignment_on_a_route_is_reported():
    result = check(*one_parcel_plan(weight_kg=-1))

    assert "not_plannable" in rules(result, "hard")


def test_a_held_consignment_may_be_listed_as_unassigned():
    result = check([parcel("C1", weight_kg=-1)], plan([], ["C1"]))

    assert result["valid"] is True


def test_a_consignment_with_no_location_on_a_route_is_reported():
    parcels = [parcel("C1", location=None)]

    result = check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1"])])]))

    assert "no_location" in rules(result, "hard")


def test_a_vehicle_on_two_routes_is_reported():
    parcels = [parcel("C1"), parcel("C2")]
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"])]), route("R2", "D2", "VAN", [stop(1, ["C2"])])])

    assert "vehicle_reused" in rules(check(parcels, the_plan), "hard")


def test_a_driver_on_two_routes_is_reported():
    parcels = [parcel("C1"), parcel("C2")]
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"])]), route("R2", "D1", "VAN2", [stop(1, ["C2"])])])

    assert "driver_reused" in rules(check(parcels, the_plan), "hard")


def test_an_empty_route_is_reported():
    assert "empty_route" in rules(check([parcel("C1")], plan([route("R1", "D1", "VAN", [])], ["C1"])), "hard")


def test_a_driver_on_leave_is_reported():
    assert "driver_unavailable" in rules(check(*one_parcel_plan(driver="A1")), "hard")


def test_a_vehicle_in_maintenance_is_reported():
    assert "vehicle_unavailable" in rules(check(*one_parcel_plan(vehicle="OFF")), "hard")


def test_an_unknown_driver_and_vehicle_are_reported():
    result = check(*one_parcel_plan(driver="X", vehicle="Y"))

    assert "driver_unavailable" in rules(result, "hard")
    assert "vehicle_unavailable" in rules(result, "hard")


def test_a_licence_that_does_not_allow_the_vehicle_is_reported():
    assert "licence" in rules(check(*one_parcel_plan(driver="M1", vehicle="VAN")), "hard")
    assert "licence" not in rules(check(*one_parcel_plan(driver="M1", vehicle="BIKE")))


def test_too_much_weight_is_reported():
    result = check(*one_parcel_plan(weight_kg=151, length_cm=50, width_cm=50, height_cm=50, vehicle="CAR"))

    assert "overweight" in rules(result, "hard")


def test_weight_at_the_limit_is_allowed():
    result = check(*one_parcel_plan(weight_kg=150, length_cm=50, width_cm=50, height_cm=50, vehicle="CAR"))

    assert "overweight" not in rules(result)


def test_too_much_volume_is_reported():
    result = check(*one_parcel_plan(weight_kg=20, length_cm=100, width_cm=100, height_cm=50, vehicle="CAR"))

    assert "over_volume" in rules(result, "hard")


def test_volume_counts_every_piece():
    result = check(*one_parcel_plan(weight_kg=9, length_cm=50, width_cm=50, height_cm=50, pieces=4, vehicle="CAR"))

    assert "over_volume" in rules(result, "hard")


def test_a_chilled_parcel_on_a_normal_vehicle_is_reported():
    assert "chilled" in rules(check(*one_parcel_plan(handling="CHILLED")), "hard")
    assert "chilled" not in rules(check(*one_parcel_plan(handling="CHILLED", vehicle="COLD")))


def test_a_heavy_item_on_the_wrong_vehicle_is_reported():
    heavy = {"weight_kg": 40, "length_cm": 80, "width_cm": 60, "height_cm": 60}

    assert "heavy_item" in rules(check(*one_parcel_plan(**heavy)), "hard")
    assert "heavy_item" not in rules(check(*one_parcel_plan(**heavy, vehicle="BIG")))


def test_item_weight_is_per_piece():
    result = check(*one_parcel_plan(weight_kg=40, pieces=2, length_cm=40, width_cm=40, height_cm=40))

    assert "heavy_item" not in rules(result)


def test_a_heavy_item_on_a_motorbike_is_reported():
    result = check(*one_parcel_plan(weight_kg=12, length_cm=30, width_cm=30, height_cm=30, vehicle="BIKE", driver="M1"))

    assert "item_limit" in rules(result, "hard")


def test_a_long_item_on_a_motorbike_is_reported():
    result = check(*one_parcel_plan(length_cm=61, width_cm=10, height_cm=10, vehicle="BIKE", driver="M1"))

    assert "item_limit" in rules(result, "hard")


def test_too_much_cash_on_a_route_is_reported():
    parcels = [
        parcel("C1", payment_type="COD", cod_amount_aed=3000),
        parcel("C2", payment_type="COD", cod_amount_aed=2500),
    ]

    assert "cash_limit" in rules(check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C2"])])])), "hard")


def test_cash_at_the_limit_is_allowed():
    parcels = [
        parcel("C1", payment_type="COD", cod_amount_aed=2500),
        parcel("C2", payment_type="COD", cod_amount_aed=2500),
    ]

    assert "cash_limit" not in rules(check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C2"])])])))


def test_a_stop_mixing_two_places_is_reported():
    parcels = [parcel("C1"), parcel("C2", TEN_KM_NORTH)]

    assert "stop_location" in rules(check(parcels, plan([route("R1", "D1", "VAN", [stop(1, ["C1", "C2"])])])), "hard")


def test_departing_before_the_shift_starts_is_reported():
    assert "early_departure" in rules(check(*one_parcel_plan(driver="L1")), "hard")
    assert "early_departure" not in rules(check(*one_parcel_plan(driver="L1", depart="12:00")))


def test_departing_before_delivery_hours_is_reported():
    assert "early_departure" in rules(check(*one_parcel_plan(depart="08:30")), "hard")


def test_a_delivery_after_hours_is_reported():
    result = check(*one_parcel_plan(driver="L1", depart="20:55", location=TEN_KM_NORTH))

    assert "delivery_hours" in rules(result, "hard")


def test_a_return_after_delivery_hours_is_allowed():
    result = check(*one_parcel_plan(driver="L1", depart="20:50"))

    assert "delivery_hours" not in rules(result)


def test_working_over_the_limit_is_reported():
    parcels = [parcel("C1", slot_start="17:00", slot_end="21:00")]
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"])], depart="19:30")])

    assert "working_time" in rules(check(parcels, the_plan), "hard")


def test_riding_during_an_active_ban_is_reported():
    parcels = [parcel("C1", TEN_KM_NORTH)]
    july = plan([route("R1", "M1", "BIKE", [stop(1, ["C1"], TEN_KM_NORTH)], depart="13:00")], plan_date="2026-07-15")
    settings, shift, assessments = world(parcels)
    shift["plan_date"] = "2026-07-15"

    assert "riding_ban" in rules(check_plan(settings, shift, assessments, july), "hard")


def test_the_ban_does_not_apply_out_of_season():
    parcels = [parcel("C1", TEN_KM_NORTH)]
    october = plan([route("R1", "M1", "BIKE", [stop(1, ["C1"], TEN_KM_NORTH)], depart="13:00")])

    assert "riding_ban" not in rules(check(parcels, october))


def test_the_ban_does_not_apply_to_other_vehicles():
    parcels = [parcel("C1", TEN_KM_NORTH)]
    settings, shift, assessments = world(parcels)
    shift["plan_date"] = "2026-07-15"
    july = plan([route("R1", "D1", "VAN", [stop(1, ["C1"], TEN_KM_NORTH)], depart="13:00")], plan_date="2026-07-15")

    assert "riding_ban" not in rules(check_plan(settings, shift, assessments, july))


def test_a_rider_already_at_a_stop_can_finish_delivering():
    parcels = [parcel("C1", TEN_KM_NORTH)]
    settings, shift, assessments = world(parcels)
    shift["plan_date"] = "2026-07-15"
    # Arrives shortly before 12:30, and delivers after the ban starts.
    july = plan([route("R1", "M1", "BIKE", [stop(1, ["C1"], TEN_KM_NORTH)], depart="11:59")], plan_date="2026-07-15")
    result = check_plan(settings, shift, assessments, july)

    assert "riding_ban" in rules(result)  # The trip back still crosses the ban.
    assert all("leg 1" not in v["message"] for v in result["violations"] if v["rule"] == "riding_ban")


# Soft rules


def test_a_late_delivery_costs_the_penalty_per_parcel():
    result = check(*one_parcel_plan(slot_start="09:00", slot_end="13:00", depart="14:00"))

    late = [v for v in result["violations"] if v["rule"] == "late"]
    assert late[0]["severity"] == "soft"
    assert late[0]["cost"] == 5
    assert result["metrics"]["late"]["count"] == 1
    assert result["metrics"]["late"]["penalty"] == 5
    assert result["valid"] is True


def test_delivery_beginning_exactly_at_the_end_of_the_slot_is_on_time():
    settings, shift, assessments = world([parcel("C1", HUB, slot_start="09:00", slot_end="13:00")])
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"], HUB)], depart="13:00")])

    assert "late" not in rules(check_plan(settings, shift, assessments, the_plan))


def test_overtime_is_charged_by_the_minute():
    settings, shift, assessments = world([parcel("C1", HUB, slot_start="17:00", slot_end="21:00")])
    the_plan = plan([route("R1", "D1", "VAN", [stop(1, ["C1"], HUB)], depart="18:07")])

    result = check_plan(settings, shift, assessments, the_plan)

    # Departs 18:07, stop takes 8 minutes, then the 30-minute break, so back at 18:45.
    overtime = next(v for v in result["violations"] if v["rule"] == "overtime")
    assert overtime["severity"] == "soft"
    assert overtime["cost"] == pytest.approx(30)


def test_an_unassigned_consignment_is_listed_with_its_reason_and_no_cost():
    result = check([parcel("C1")], plan([], ["C1"]))

    unassigned = next(v for v in result["violations"] if v["rule"] == "unassigned")
    assert unassigned["severity"] == "soft"
    assert unassigned["cost"] == 0
    assert "Test" in unassigned["message"]
    assert result["metrics"]["unassigned"] == 1


def test_every_violation_is_listed_together():
    parcels = [parcel("C1", weight_kg=160, length_cm=50, width_cm=50, height_cm=50, handling="CHILLED")]

    result = check(parcels, plan([route("R1", "M1", "CAR", [stop(1, ["C1"])])]))

    assert {"licence", "overweight", "chilled", "heavy_item"} <= set(rules(result, "hard"))


# Metrics


def test_metrics_match_totals_worked_out_by_hand():
    parcels = [
        parcel("C1", TEN_KM_NORTH),
        parcel("C2", HUB, slot_start="09:00", slot_end="13:00", quality="INCOMPLETE"),
        parcel("C3"),
    ]
    the_plan = plan(
        [
            route("R1", "D1", "VAN", [stop(1, ["C1"], TEN_KM_NORTH)]),
            route("R2", "D2", "CAR", [stop(1, ["C2"], HUB)], depart="14:00"),
        ],
        ["C3"],
    )

    metrics = check(parcels, the_plan)["metrics"]

    # R1: fixed 220 + 28 km × 0.70 = 239.6. R2: fixed 140, no distance, late penalty 5.
    assert metrics["total_cost"] == pytest.approx(384.6, abs=0.05)
    assert metrics["routed"] == 2
    assert metrics["unassigned"] == 1
    assert metrics["cost_per_routed"] == pytest.approx(192.3, abs=0.05)
    assert metrics["on_time"] == 1
    assert metrics["distance_km"] == pytest.approx(28.0, abs=0.05)
    assert metrics["overtime"] == {"minutes": 0, "cost": 0}
    assert metrics["workload"] == {"earliest_return": "10:14", "latest_return": "14:08", "gap_minutes": 234}
    assert metrics["risky_stops"] == {"INCOMPLETE": 1}


def test_a_plan_with_nothing_routed_has_empty_metrics():
    metrics = check([parcel("C1")], plan([], ["C1"]))["metrics"]

    assert metrics["routed"] == 0
    assert metrics["cost_per_routed"] is None
    assert metrics["workload"] == {"earliest_return": None, "latest_return": None, "gap_minutes": None}
