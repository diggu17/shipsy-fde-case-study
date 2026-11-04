"""Checks one client's settings and reports every problem found."""

import math
import re
from dataclasses import dataclass

_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_TIME_PATTERN = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")

ID = "id"
TEXT = "text"
TIME = "time"
BOOL = "bool"
POSITIVE = "positive"
NON_NEGATIVE = "non_negative"
MONTH = "month"
LATITUDE = "latitude"
LONGITUDE = "longitude"


@dataclass(frozen=True)
class Field:
    kind: str
    label: str
    required: bool = True


@dataclass(frozen=True)
class Section:
    fields: dict
    label: str


@dataclass(frozen=True)
class Items:
    fields: dict
    label: str
    allow_empty: bool = False


@dataclass(frozen=True)
class ValueMap:
    kind: str
    label: str
    allow_empty: bool = False
    key_kind: str = ID


@dataclass(frozen=True)
class IdList:
    label: str


@dataclass(frozen=True)
class TextList:
    label: str


_WINDOW = {"start": Field(TIME, "Start time"), "end": Field(TIME, "End time")}

PAYMENT_TYPES = ("PREPAID", "COD")
HANDLING_TYPES = ("NONE", "FRAGILE", "CHILLED", "BULKY")
DRIVER_STATUSES = ("AVAILABLE", "ON_LEAVE")
VEHICLE_STATUSES = ("AVAILABLE", "IN_MAINTENANCE")


def _column(label, required=True):
    return Field(TEXT, f"{label} column", required=required)


_UPLOAD = Section(
    {
        "column_mappings": Section(
            {
                "consignments": Section(
                    {
                        "consignment_id": _column("Consignment ID"),
                        "shop_id": _column("Shop ID", required=False),
                        "customer_phone": _column("Customer phone"),
                        "address_line_1": _column("Address line 1"),
                        "address_line_2": _column("Address line 2", required=False),
                        "area": _column("Area"),
                        "city": _column("City"),
                        "landmark": _column("Landmark", required=False),
                        "weight_kg": _column("Weight"),
                        "length_cm": _column("Length"),
                        "width_cm": _column("Width"),
                        "height_cm": _column("Height"),
                        "pieces": _column("Pieces"),
                        "slot_start": _column("Slot start"),
                        "slot_end": _column("Slot end"),
                        "payment_type": _column("Payment type"),
                        "cod_amount": _column("COD amount"),
                        "handling": _column("Handling"),
                        "attempt_number": _column("Attempt number"),
                        "notes": _column("Notes", required=False),
                    },
                    "Consignment columns",
                ),
                "drivers": Section(
                    {
                        "driver_id": _column("Driver ID"),
                        "name": _column("Driver name", required=False),
                        "licence": _column("Licence"),
                        "shift_start": _column("Shift start"),
                        "shift_end": _column("Shift end"),
                        "experience_months": _column("Experience", required=False),
                        "status": _column("Driver status"),
                    },
                    "Driver columns",
                ),
                "vehicles": Section(
                    {
                        "vehicle_id": _column("Vehicle ID"),
                        "type": _column("Vehicle type"),
                        "max_weight_kg": _column("Vehicle weight limit", required=False),
                        "max_volume_m3": _column("Vehicle volume limit", required=False),
                        "refrigerated": _column("Refrigerated", required=False),
                        "fixed_cost_per_day": _column("Vehicle fixed cost", required=False),
                        "cost_per_km": _column("Vehicle cost per km", required=False),
                        "status": _column("Vehicle status"),
                    },
                    "Vehicle columns",
                ),
                "delivery_history": Section(
                    {
                        "address_line_1": _column("History address line 1"),
                        "address_line_2": _column("History address line 2", required=False),
                        "area": _column("History area"),
                        "city": _column("History city"),
                        "customer_phone": _column("History phone"),
                        "failure_reason": _column("Failure reason"),
                        "event_lat": _column("Event latitude"),
                        "event_lng": _column("Event longitude"),
                    },
                    "Delivery history columns",
                ),
                "areas": Section(
                    {
                        "area_name": _column("Area name"),
                        "aliases": _column("Area aliases", required=False),
                        "emirate": _column("Area region"),
                        "centroid_lat": _column("Area latitude"),
                        "centroid_lng": _column("Area longitude"),
                    },
                    "Area columns",
                ),
                "verified_locations": Section(
                    {
                        "consignment_id": _column("Verified consignment ID"),
                        "true_lat": _column("Verified latitude"),
                        "true_lng": _column("Verified longitude"),
                    },
                    "Verified location columns",
                ),
            },
            "Column mappings",
        ),
        "value_mappings": Section(
            {
                "licence": ValueMap(ID, "Licence label", True, TEXT),
                "vehicle_type": ValueMap(ID, "Vehicle type label", True, TEXT),
                "payment_type": ValueMap(ID, "Payment type label", True, TEXT),
                "handling": ValueMap(ID, "Handling label", True, TEXT),
                "driver_status": ValueMap(ID, "Driver status label", True, TEXT),
                "vehicle_status": ValueMap(ID, "Vehicle status label", True, TEXT),
            },
            "Value mappings",
        ),
        "test_row_markers": TextList("Test row markers"),
        "max_kg_per_m3": Field(POSITIVE, "Maximum weight per cubic metre"),
        "weight_unit_limit_kg": Field(POSITIVE, "Weight unit limit"),
        "default_box_cm": Section(
            {
                "length": Field(POSITIVE, "Default box length"),
                "width": Field(POSITIVE, "Default box width"),
                "height": Field(POSITIVE, "Default box height"),
            },
            "Default box size",
        ),
    },
    "Upload settings",
)

_CONFIDENCE_LEVELS = (
    "confirmed",
    "phone_area_many",
    "phone_area_few",
    "same_place",
    "phone_only",
    "disagreeing",
    "area_centre",
    "geocoded",
)

GEOCODERS = ("nominatim", "google", "none")

_ADDRESS = Section(
    {
        "service_area": TextList("Service area"),
        "alias_separator": Field(TEXT, "Alias separator"),
        "location_code_word": Field(TEXT, "Location code word", required=False),
        "max_spread_m": Field(POSITIVE, "Maximum spread"),
        "hub_exclusion_m": Field(POSITIVE, "Hub exclusion distance"),
        "hub_area_radius_m": Field(POSITIVE, "Hub area radius"),
        "confidence": Section(
            {level: Field(NON_NEGATIVE, "Confidence") for level in _CONFIDENCE_LEVELS},
            "Confidence values",
        ),
        "word_replacements": ValueMap(TEXT, "Word replacement", True, TEXT),
        "villa_words": TextList("Villa words"),
        "vague_phrases": TextList("Vague phrases"),
        "unusable_text": TextList("Unusable text"),
        "note_address_markers": TextList("Note address markers"),
        "unreliable_failures": TextList("Unreliable failures"),
        "note_conflicts": Items(
            {
                "phrase": Field(TEXT, "Note phrase"),
                "start_before": Field(TIME, "Start before", required=False),
                "end_after": Field(TIME, "End after", required=False),
            },
            "Note conflicts",
            allow_empty=True,
        ),
        "cod_note_conflicts": TextList("COD note conflicts"),
        "completeness": Section(
            {
                "unit_words": TextList("Unit words"),
                "street_unit_words": TextList("Street unit words"),
                "number_prefixes": TextList("Number prefixes"),
                "max_unit_digits": Field(POSITIVE, "Maximum unit digits"),
                "not_building_words": TextList("Not building words"),
            },
            "Address completeness",
        ),
        "verification": Section(
            {
                "geocoder": Field(TEXT, "Geocoding provider"),
                "country_code": Field(TEXT, "Country code"),
                "max_area_distance_m": Field(POSITIVE, "Distance from the area centre allowed"),
                "min_centre_distance_m": Field(POSITIVE, "Too close to the area centre"),
                "repeat_failure_threshold": Field(POSITIVE, "Repeated failure threshold"),
                "message": Field(TEXT, "Confirmation message"),
                "area_question": Field(TEXT, "Area question"),
                "which_area_question": Field(TEXT, "Which area question"),
                "missing_question": Field(TEXT, "Missing detail question"),
                "door_question": Field(TEXT, "Door question"),
            },
            "Address verification",
        ),
    },
    "Address settings",
)

_SCHEMA = {
    "client_id": Field(ID, "Client ID"),
    "name": Field(TEXT, "Client name"),
    "currency": Field(TEXT, "Currency"),
    "hubs": Items(
        {
            "hub_id": Field(ID, "Hub ID"),
            "name": Field(TEXT, "Hub name"),
            "lat": Field(LATITUDE, "Hub latitude"),
            "lng": Field(LONGITUDE, "Hub longitude"),
        },
        "Hubs",
    ),
    "day": Section(
        {
            "plan_deadline": Field(TIME, "Plan deadline"),
            "delivery_start": Field(TIME, "Delivery start"),
            "delivery_end": Field(TIME, "Delivery end"),
        },
        "Day settings",
    ),
    "slots": Section(
        {
            "any_time_offered": Field(BOOL, "Any time option"),
            "windows": Items(_WINDOW, "Slot windows", allow_empty=True),
        },
        "Slots",
    ),
    "vehicle_types": Items(
        {
            "type_id": Field(ID, "Vehicle type ID"),
            "name": Field(TEXT, "Vehicle type name"),
            "max_weight_kg": Field(POSITIVE, "Weight limit"),
            "max_volume_m3": Field(POSITIVE, "Volume limit"),
            "fixed_cost_per_day": Field(NON_NEGATIVE, "Fixed cost"),
            "cost_per_km": Field(NON_NEGATIVE, "Cost per km"),
            "max_item_weight_kg": Field(POSITIVE, "Item weight limit", required=False),
            "max_item_length_cm": Field(POSITIVE, "Item length limit", required=False),
            "has_helper": Field(BOOL, "Helper option", required=False),
            "refrigerated": Field(BOOL, "Refrigerated option", required=False),
            "required_for_items_over_kg": Field(POSITIVE, "Heavy item threshold", required=False),
        },
        "Vehicle types",
    ),
    "licences": Items(
        {
            "licence_id": Field(ID, "Licence ID"),
            "vehicle_types": IdList("Licence vehicle types"),
        },
        "Licences",
    ),
    "driver_rules": Section(
        {
            "max_working_hours": Field(POSITIVE, "Maximum working hours"),
            "break_minutes": Field(NON_NEGATIVE, "Break length"),
            "break_required_over_hours": Field(POSITIVE, "Break threshold"),
            "break_after_hours": Field(POSITIVE, "Break timing"),
            "overtime_rate_per_hour": Field(NON_NEGATIVE, "Overtime rate"),
            "cash_limit": Field(POSITIVE, "Cash limit"),
        },
        "Driver rules",
    ),
    "dated_rules": Items(
        {
            "vehicle_type": Field(ID, "Dated rule vehicle type"),
            "from_month": Field(MONTH, "Dated rule first month"),
            "to_month": Field(MONTH, "Dated rule last month"),
            "no_riding_start": Field(TIME, "No-riding start"),
            "no_riding_end": Field(TIME, "No-riding end"),
        },
        "Dated rules",
        allow_empty=True,
    ),
    "travel": Section(
        {
            "speed_offpeak_kmh": Field(POSITIVE, "Off-peak speed"),
            "speed_peak_kmh": Field(POSITIVE, "Peak speed"),
            "peak_windows": Items(_WINDOW, "Peak windows", allow_empty=True),
            "vehicle_speed_factors": ValueMap(POSITIVE, "Vehicle speed factor", allow_empty=True),
            "road_distance_factor": Field(POSITIVE, "Road distance factor"),
        },
        "Travel settings",
    ),
    "stop_time": Section(
        {
            "minutes_by_stop_type": ValueMap(NON_NEGATIVE, "Stop time"),
            "cod_extra_minutes": Field(NON_NEGATIVE, "COD extra time"),
            "extra_parcel_minutes": Field(NON_NEGATIVE, "Extra parcel time"),
            "gate_extra_minutes": Field(NON_NEGATIVE, "Gate extra time"),
            "stop_type_words": Items(
                {"stop_type": Field(ID, "Stop type"), "words": TextList("Stop type words")},
                "Stop type words",
                allow_empty=True,
            ),
            "default_stop_type": Field(ID, "Default stop type"),
            "gated_areas": TextList("Gated areas"),
            "gate_note_phrases": TextList("Gate note phrases"),
            "stop_radius_m": Field(POSITIVE, "Stop location limit"),
        },
        "Stop time settings",
    ),
    "money": Section(
        {
            "revenue_per_delivery": Field(NON_NEGATIVE, "Revenue per delivery"),
            "failed_attempt_cost": Field(NON_NEGATIVE, "Failed attempt cost"),
            "late_penalty_per_parcel": Field(NON_NEGATIVE, "Late penalty"),
        },
        "Money settings",
    ),
    "upload": _UPLOAD,
    "address": _ADDRESS,
    "planning": Section(
        {
            "late_weight": Field(NON_NEGATIVE, "Late weight"),
            "unassigned_penalty": Field(NON_NEGATIVE, "Unassigned penalty"),
            "workload_weight": Field(NON_NEGATIVE, "Workload weight"),
            "solver_seconds": Field(POSITIVE, "Solver time limit"),
            "handover_minutes": Field(NON_NEGATIVE, "Handover time"),
            "failure_rates": Section(
                {
                    "with_area": Field(NON_NEGATIVE, "Failure rate with an area"),
                    "without_area": Field(NON_NEGATIVE, "Failure rate without an area"),
                },
                "Failure rates",
            ),
            "assumptions": TextList("Plan assumptions"),
        },
        "Planning settings",
    ),
}


def check_settings(document):
    """Return every problem in a client's settings. An empty list means they are valid."""
    problems = []
    if not isinstance(document, dict):
        _add(problems, "", "Settings must be an object")
        return problems
    _check_fields(_SCHEMA, document, "", problems)
    _check_relations(document, problems)
    return problems


def active_dated_rules(settings, plan_date):
    """Return the client's dated rules that apply on the plan date."""
    return [rule for rule in settings["dated_rules"] if _month_in_range(plan_date.month, rule)]


def _month_in_range(month, rule):
    first, last = rule["from_month"], rule["to_month"]
    if first <= last:
        return first <= month <= last
    return month >= first or month <= last


def _add(problems, path, message):
    problems.append({"path": path, "message": message})


def _join(path, key):
    return f"{path}.{key}" if path else key


def _check_fields(schema, values, path, problems):
    for key in values:
        if key not in schema:
            _add(problems, _join(path, key), "Unknown field")
    for key, rule in schema.items():
        child = _join(path, key)
        if key not in values:
            if getattr(rule, "required", True):
                _add(problems, child, f"{rule.label} is required")
        else:
            _check_rule(rule, values[key], child, problems)


def _check_rule(rule, value, path, problems):
    if isinstance(rule, Field):
        _check_value(rule.kind, rule.label, value, path, problems)
    elif isinstance(rule, Section):
        if isinstance(value, dict):
            _check_fields(rule.fields, value, path, problems)
        else:
            _add(problems, path, f"{rule.label} must be an object")
    elif isinstance(rule, Items):
        _check_items(rule, value, path, problems)
    elif isinstance(rule, ValueMap):
        _check_value_map(rule, value, path, problems)
    elif isinstance(rule, IdList):
        if not isinstance(value, list) or not value:
            _add(problems, path, f"{rule.label} must be a list that isn't empty")
        else:
            for index, item in enumerate(value):
                _check_value(ID, rule.label, item, f"{path}[{index}]", problems)
    elif isinstance(rule, TextList):
        if not isinstance(value, list):
            _add(problems, path, f"{rule.label} must be a list")
        else:
            for index, item in enumerate(value):
                _check_value(TEXT, rule.label, item, f"{path}[{index}]", problems)


def _check_items(rule, value, path, problems):
    if not isinstance(value, list):
        _add(problems, path, f"{rule.label} must be a list")
    elif not value and not rule.allow_empty:
        _add(problems, path, f"{rule.label} must not be empty")
    else:
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            if isinstance(item, dict):
                _check_fields(rule.fields, item, child, problems)
            else:
                _add(problems, child, f"{rule.label} entries must be objects")


def _check_value_map(rule, value, path, problems):
    if not isinstance(value, dict):
        _add(problems, path, f"{rule.label} must be an object")
    elif not value and not rule.allow_empty:
        _add(problems, path, f"{rule.label} must not be empty")
    else:
        for key, item in value.items():
            _check_value(rule.key_kind, f"{rule.label} name", key, _join(path, key), problems)
            _check_value(rule.kind, rule.label, item, _join(path, key), problems)


def _check_value(kind, label, value, path, problems):
    message = _value_problem(kind, label, value)
    if message:
        _add(problems, path, message)


def _value_problem(kind, label, value):
    if kind == ID:
        if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
            return f"{label} must use only letters, numbers, hyphens, and underscores"
    elif kind == TEXT:
        if not isinstance(value, str) or not value.strip():
            return f"{label} must be text that isn't empty"
    elif kind == BOOL:
        if not isinstance(value, bool):
            return f"{label} must be true or false"
    elif kind == TIME:
        if time_to_minutes(value) is None:
            return f"{label} must be a time in HH:MM format"
    elif kind == LATITUDE or kind == LONGITUDE:
        limit = 90 if kind == LATITUDE else 180
        if not _is_number(value) or abs(value) > limit:
            return "Hub location isn't a valid coordinate"
    elif not _is_number(value):
        return f"{label} must be a number"
    elif kind == POSITIVE and value <= 0:
        return f"{label} must be above zero"
    elif kind == NON_NEGATIVE and value < 0:
        return f"{label} must not be negative"
    elif kind == MONTH and (not isinstance(value, int) or not 1 <= value <= 12):
        return f"{label} must be a month from 1 to 12"
    return None


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def time_to_minutes(value):
    """Convert HH:MM to minutes since midnight, or None if it isn't a valid time."""
    match = _TIME_PATTERN.fullmatch(value) if isinstance(value, str) else None
    return int(match[1]) * 60 + int(match[2]) if match else None


def _section(document, key):
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def _entries(values, key):
    value = values.get(key)
    if not isinstance(value, list):
        return []
    return [(f"{key}[{index}]", item) for index, item in enumerate(value) if isinstance(item, dict)]


def _check_relations(document, problems):
    _check_unique(document, "hubs", "hub_id", "Hub ID", problems)
    _check_unique(document, "vehicle_types", "type_id", "Vehicle type ID", problems)
    _check_unique(document, "licences", "licence_id", "Licence ID", problems)
    _check_vehicle_references(document, problems)
    _check_day(document, problems)
    _check_slots(document, problems)
    _check_time_ranges(document, problems)
    _check_value_mappings(document, problems)
    _check_address(document, problems)
    _check_stop_types(document, problems)

    factor = _section(document, "travel").get("road_distance_factor")
    if _is_number(factor) and 0 < factor < 1:
        _add(problems, "travel.road_distance_factor", "Road distance factor must be at least 1")


def _check_unique(document, key, id_field, label, problems):
    seen = set()
    for path, entry in _entries(document, key):
        value = entry.get(id_field)
        if isinstance(value, str):
            if value in seen:
                _add(problems, f"{path}.{id_field}", f"{label} must be unique")
            seen.add(value)


def _check_vehicle_references(document, problems):
    known = {entry.get("type_id") for _, entry in _entries(document, "vehicle_types")}
    if not known:
        return

    def check(path, value):
        if isinstance(value, str) and value not in known:
            _add(problems, path, "Unknown vehicle type")

    for path, licence in _entries(document, "licences"):
        allowed = licence.get("vehicle_types")
        if isinstance(allowed, list):
            for index, value in enumerate(allowed):
                check(f"{path}.vehicle_types[{index}]", value)
    for path, rule in _entries(document, "dated_rules"):
        check(f"{path}.vehicle_type", rule.get("vehicle_type"))
    factors = _section(document, "travel").get("vehicle_speed_factors")
    if isinstance(factors, dict):
        for key in factors:
            check(f"travel.vehicle_speed_factors.{key}", key)


def _check_value_mappings(document, problems):
    mappings = _section(_section(document, "upload"), "value_mappings")
    vehicle_types = {entry.get("type_id") for _, entry in _entries(document, "vehicle_types")}
    licences = {entry.get("licence_id") for _, entry in _entries(document, "licences")}
    targets = {
        "licence": (licences, "Unknown licence"),
        "vehicle_type": (vehicle_types, "Unknown vehicle type"),
        "payment_type": (PAYMENT_TYPES, "Unknown payment type"),
        "handling": (HANDLING_TYPES, "Unknown handling type"),
        "driver_status": (DRIVER_STATUSES, "Unknown driver status"),
        "vehicle_status": (VEHICLE_STATUSES, "Unknown vehicle status"),
    }
    for key, (allowed, message) in targets.items():
        mapping = mappings.get(key)
        if isinstance(mapping, dict):
            for label, target in mapping.items():
                if isinstance(target, str) and target not in allowed:
                    _add(problems, f"upload.value_mappings.{key}.{label}", message)


def _check_address(document, problems):
    address = _section(document, "address")
    for level, value in _section(address, "confidence").items():
        if _is_number(value) and value > 1:
            _add(problems, f"address.confidence.{level}", "Confidence must be between 0 and 1")
    for path, rule in _entries(address, "note_conflicts"):
        if "start_before" not in rule and "end_after" not in rule:
            _add(problems, f"address.{path}", "Note conflict needs start_before or end_after")
    geocoder = _section(address, "verification").get("geocoder")
    if isinstance(geocoder, str) and geocoder not in GEOCODERS:
        _add(problems, "address.verification.geocoder", "Unknown geocoding provider")


def _check_stop_types(document, problems):
    stop_time = _section(document, "stop_time")
    known = stop_time.get("minutes_by_stop_type")
    if not isinstance(known, dict):
        return
    default = stop_time.get("default_stop_type")
    if isinstance(default, str) and default not in known:
        _add(problems, "stop_time.default_stop_type", "Stop type has no time set")
    for path, entry in _entries(stop_time, "stop_type_words"):
        stop_type = entry.get("stop_type")
        if isinstance(stop_type, str) and stop_type not in known:
            _add(problems, f"stop_time.{path}.stop_type", "Stop type has no time set")


def _check_day(document, problems):
    day = _section(document, "day")
    deadline = time_to_minutes(day.get("plan_deadline"))
    start = time_to_minutes(day.get("delivery_start"))
    end = time_to_minutes(day.get("delivery_end"))
    if start is not None and end is not None and end <= start:
        _add(problems, "day.delivery_end", "Delivery hours end before they start")
    if deadline is not None and start is not None and deadline >= start:
        _add(problems, "day.plan_deadline", "Plan deadline must be before delivery hours begin")


def _check_slots(document, problems):
    slots = _section(document, "slots")
    day = _section(document, "day")
    open_at = time_to_minutes(day.get("delivery_start"))
    close_at = time_to_minutes(day.get("delivery_end"))

    if slots.get("windows") == [] and slots.get("any_time_offered") is False:
        _add(problems, "slots.windows", "At least one slot must be offered")

    valid = []
    for path, window in _entries(slots, "windows"):
        start = time_to_minutes(window.get("start"))
        end = time_to_minutes(window.get("end"))
        if start is None or end is None:
            continue
        if end <= start:
            _add(problems, f"slots.{path}", "Slot ends before it starts")
            continue
        valid.append((start, end))
        if (open_at is not None and start < open_at) or (close_at is not None and end > close_at):
            _add(problems, f"slots.{path}", "Slot is outside delivery hours")

    latest_end = None
    for start, end in sorted(valid):
        if latest_end is not None and start < latest_end:
            _add(problems, "slots.windows", "Slots overlap")
        latest_end = end if latest_end is None else max(latest_end, end)


def _check_time_ranges(document, problems):
    for path, window in _entries(_section(document, "travel"), "peak_windows"):
        _check_range(window, "start", "end", f"travel.{path}", "Peak window", problems)
    for path, rule in _entries(document, "dated_rules"):
        _check_range(rule, "no_riding_start", "no_riding_end", path, "No-riding time", problems)


def _check_range(values, start_key, end_key, path, label, problems):
    start = time_to_minutes(values.get(start_key))
    end = time_to_minutes(values.get(end_key))
    if start is not None and end is not None and end <= start:
        _add(problems, path, f"{label} ends before it starts")
