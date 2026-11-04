from dispatch_planner.settings import check_settings


def messages(problems):
    return [p["message"] for p in problems]


def paths(problems):
    return [p["path"] for p in problems]


def test_sahras_complete_settings_have_no_problems(sahra):
    assert check_settings(sahra) == []


def test_missing_overtime_rate_is_reported(sahra):
    del sahra["driver_rules"]["overtime_rate_per_hour"]

    problems = check_settings(sahra)

    assert messages(problems) == ["Overtime rate is required"]
    assert paths(problems) == ["driver_rules.overtime_rate_per_hour"]


def test_misspelled_field_reports_both_the_unknown_field_and_the_missing_one(sahra):
    rules = sahra["driver_rules"]
    rules["overtme_rate"] = rules.pop("overtime_rate_per_hour")

    problems = check_settings(sahra)

    assert sorted(messages(problems)) == ["Overtime rate is required", "Unknown field"]
    assert "driver_rules.overtme_rate" in paths(problems)


def test_slot_ending_before_it_starts_is_reported(sahra):
    sahra["slots"]["windows"] = [{"start": "17:00", "end": "13:00"}]

    assert "Slot ends before it starts" in messages(check_settings(sahra))


def test_overlapping_slots_are_reported(sahra):
    sahra["slots"]["windows"] = [
        {"start": "09:00", "end": "13:00"},
        {"start": "12:00", "end": "17:00"},
    ]

    assert messages(check_settings(sahra)) == ["Slots overlap"]


def test_slots_that_only_touch_do_not_overlap(sahra):
    sahra["slots"]["windows"] = [
        {"start": "09:00", "end": "13:00"},
        {"start": "13:00", "end": "17:00"},
    ]

    assert check_settings(sahra) == []


def test_slot_outside_delivery_hours_is_reported(sahra):
    sahra["slots"]["windows"] = [{"start": "07:00", "end": "08:30"}]

    assert messages(check_settings(sahra)) == ["Slot is outside delivery hours"]


def test_plan_deadline_after_delivery_start_is_reported(sahra):
    sahra["day"]["plan_deadline"] = "09:30"

    assert messages(check_settings(sahra)) == ["Plan deadline must be before delivery hours begin"]


def test_invalid_time_is_reported(sahra):
    sahra["day"]["delivery_end"] = "25:00"

    assert messages(check_settings(sahra)) == ["Delivery end must be a time in HH:MM format"]


def test_negative_weight_limit_is_reported(sahra):
    sahra["vehicle_types"][2]["max_weight_kg"] = -600

    problems = check_settings(sahra)

    assert messages(problems) == ["Weight limit must be above zero"]
    assert paths(problems) == ["vehicle_types[2].max_weight_kg"]


def test_negative_cost_is_reported_but_zero_is_allowed(sahra):
    sahra["vehicle_types"][0]["cost_per_km"] = -1
    sahra["vehicle_types"][1]["cost_per_km"] = 0

    assert messages(check_settings(sahra)) == ["Cost per km must not be negative"]


def test_text_in_a_number_field_is_reported(sahra):
    sahra["money"]["late_penalty_per_parcel"] = "five"

    assert messages(check_settings(sahra)) == ["Late penalty must be a number"]


def test_true_is_not_accepted_as_a_number(sahra):
    sahra["money"]["late_penalty_per_parcel"] = True

    assert messages(check_settings(sahra)) == ["Late penalty must be a number"]


def test_invalid_hub_latitude_is_reported(sahra):
    sahra["hubs"][0]["lat"] = 125

    assert messages(check_settings(sahra)) == ["Hub location isn't a valid coordinate"]


def test_duplicate_hub_ids_are_reported(sahra):
    sahra["hubs"].append(dict(sahra["hubs"][0]))

    assert messages(check_settings(sahra)) == ["Hub ID must be unique"]


def test_duplicate_vehicle_type_ids_are_reported(sahra):
    sahra["vehicle_types"].append(dict(sahra["vehicle_types"][0]))

    assert messages(check_settings(sahra)) == ["Vehicle type ID must be unique"]


def test_licence_with_undefined_vehicle_type_is_reported(sahra):
    sahra["licences"][0]["vehicle_types"].append("TRUCK")

    assert messages(check_settings(sahra)) == ["Unknown vehicle type"]


def test_dated_rule_with_undefined_vehicle_type_is_reported(sahra):
    sahra["dated_rules"][0]["vehicle_type"] = "TRUCK"

    assert messages(check_settings(sahra)) == ["Unknown vehicle type"]


def test_road_distance_factor_below_one_is_reported(sahra):
    sahra["travel"]["road_distance_factor"] = 0.9

    assert messages(check_settings(sahra)) == ["Road distance factor must be at least 1"]


def test_a_client_needs_at_least_one_hub(sahra):
    sahra["hubs"] = []

    assert messages(check_settings(sahra)) == ["Hubs must not be empty"]


def test_a_section_of_the_wrong_type_is_reported_without_crashing(sahra):
    sahra["driver_rules"] = "none"

    assert messages(check_settings(sahra)) == ["Driver rules must be an object"]


def test_a_document_that_is_not_an_object_is_reported_without_crashing():
    assert messages(check_settings(["not", "settings"])) == ["Settings must be an object"]


def test_a_client_with_no_slots_and_no_any_time_option_is_reported(sahra):
    sahra["slots"] = {"any_time_offered": False, "windows": []}

    assert messages(check_settings(sahra)) == ["At least one slot must be offered"]


def test_a_client_offering_only_any_time_is_valid(sahra):
    sahra["slots"] = {"any_time_offered": True, "windows": []}

    assert check_settings(sahra) == []


def test_a_client_id_unsafe_for_urls_is_reported(sahra):
    sahra["client_id"] = "sahra/express"

    assert messages(check_settings(sahra)) == [
        "Client ID must use only letters, numbers, hyphens, and underscores"
    ]


def test_upload_settings_are_required(sahra):
    del sahra["upload"]

    assert messages(check_settings(sahra)) == ["Upload settings is required"]


def test_a_missing_required_column_mapping_is_reported(sahra):
    del sahra["upload"]["column_mappings"]["consignments"]["weight_kg"]

    assert messages(check_settings(sahra)) == ["Weight column is required"]


def test_optional_column_mappings_can_be_left_out(sahra):
    del sahra["upload"]["column_mappings"]["consignments"]["landmark"]

    assert check_settings(sahra) == []


def test_value_mapping_to_an_undefined_vehicle_type_is_reported(sahra):
    sahra["upload"]["value_mappings"]["vehicle_type"]["Lorry"] = "TRUCK"

    assert messages(check_settings(sahra)) == ["Unknown vehicle type"]


def test_value_mapping_to_an_undefined_licence_is_reported(sahra):
    sahra["upload"]["value_mappings"]["licence"]["heavy"] = "HGV"

    assert messages(check_settings(sahra)) == ["Unknown licence"]


def test_value_mapping_to_an_unsupported_payment_type_is_reported(sahra):
    sahra["upload"]["value_mappings"]["payment_type"]["cash"] = "CASH"

    assert messages(check_settings(sahra)) == ["Unknown payment type"]


def test_negative_weight_limit_for_uploads_is_reported(sahra):
    sahra["upload"]["max_kg_per_m3"] = -1

    assert messages(check_settings(sahra)) == ["Maximum weight per cubic metre must be above zero"]


def test_every_mistake_is_listed_together(sahra):
    sahra["slots"]["windows"] = [
        {"start": "17:00", "end": "13:00"},
        {"start": "09:00", "end": "13:00"},
        {"start": "12:00", "end": "17:00"},
    ]
    sahra["vehicle_types"][2]["max_weight_kg"] = -600
    sahra["hubs"][0]["lat"] = 125
    rules = sahra["driver_rules"]
    rules["overtme_rate"] = rules.pop("overtime_rate_per_hour")
    sahra["licences"][0]["vehicle_types"].append("TRUCK")

    found = messages(check_settings(sahra))

    for expected in (
        "Slot ends before it starts",
        "Slots overlap",
        "Weight limit must be above zero",
        "Hub location isn't a valid coordinate",
        "Overtime rate is required",
        "Unknown field",
        "Unknown vehicle type",
    ):
        assert expected in found


def test_address_settings_are_required(sahra):
    del sahra["address"]

    assert messages(check_settings(sahra)) == ["Address settings is required"]


def test_a_confidence_above_one_is_reported(sahra):
    sahra["address"]["confidence"]["area_centre"] = 1.5

    assert messages(check_settings(sahra)) == ["Confidence must be between 0 and 1"]


def test_a_note_conflict_needs_a_time_rule(sahra):
    sahra["address"]["note_conflicts"].append({"phrase": "weekend only"})

    assert messages(check_settings(sahra)) == ["Note conflict needs start_before or end_after"]


def test_a_missing_history_column_mapping_is_reported(sahra):
    del sahra["upload"]["column_mappings"]["delivery_history"]["event_lat"]

    assert messages(check_settings(sahra)) == ["Event latitude column is required"]


def test_a_default_stop_type_without_a_time_is_reported(sahra):
    sahra["stop_time"]["default_stop_type"] = "castle"

    assert messages(check_settings(sahra)) == ["Stop type has no time set"]


def test_stop_type_words_for_a_type_without_a_time_are_reported(sahra):
    sahra["stop_time"]["stop_type_words"].append({"stop_type": "castle", "words": ["castle"]})

    assert messages(check_settings(sahra)) == ["Stop type has no time set"]


def test_break_timing_is_required(sahra):
    del sahra["driver_rules"]["break_after_hours"]

    assert messages(check_settings(sahra)) == ["Break timing is required"]
