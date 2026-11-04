"""Turns a shift's CSV files into standardised rows, with a result for each row."""

import csv
import io
import math
import re

from dispatch_planner.settings import (
    DRIVER_STATUSES,
    HANDLING_TYPES,
    PAYMENT_TYPES,
    VEHICLE_STATUSES,
)

FILES = ("consignments", "drivers", "vehicles")
_ID_FIELDS = {"consignments": "consignment_id", "drivers": "driver_id", "vehicles": "vehicle_id"}
_RESULTS = ("accepted", "corrected", "flagged", "held", "rejected")
_DUPLICATE_CODES = ("duplicate_exact", "duplicate_conflict")

_TIME_24 = re.compile(r"(\d{1,2}):(\d{2})")
_TIME_AM_PM = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?")


class UploadRejected(Exception):
    def __init__(self, problems):
        super().__init__("Upload rejected")
        self.problems = problems


class RowNotFound(Exception):
    pass


class RowNotHeld(Exception):
    pass


class ResolutionRejected(Exception):
    def __init__(self, messages):
        super().__init__("Resolution rejected")
        self.messages = messages


def process_shift(settings, files):
    """Check a shift's files. Returns its rows and report, or raises UploadRejected."""
    problems = []
    raw = {name: read_rows(settings, name, files[name], problems) for name in FILES}
    if problems:
        raise UploadRejected(problems)

    shift = {name: _process_rows(settings, name, raw[name]) for name in FILES}
    shift["received"] = {name: len(raw[name]) for name in FILES}
    shift["report"] = build_report(shift)
    return shift


def build_report(shift):
    report = {"issues": []}
    for name in FILES:
        rows = shift[name]
        counts = {result: 0 for result in _RESULTS}
        for row in rows:
            counts[row["result"]] += 1
            if row["findings"]:
                report["issues"].append(
                    {
                        "file": name,
                        "id": row["id"],
                        "result": row["result"],
                        "messages": [finding["message"] for finding in row["findings"]],
                    }
                )
        report[name] = {
            "received": shift["received"][name],
            "duplicates_removed": shift["received"][name] - len(rows),
            **counts,
            "plannable": sum(row["plannable"] for row in rows),
        }
    return report


def resolve_row(settings, shift, name, row_id, action, values=None):
    """Apply the manager's decision on a held row, then refresh the report."""
    rows = shift.get(name) if name in FILES else None
    row = next((r for r in rows or [] if r["id"] == row_id), None)
    if row is None:
        raise RowNotFound
    if row["result"] != "held":
        raise RowNotHeld

    if action == "leave_out":
        row["decision"] = {"action": action}
        row["findings"].append(_finding("left_out", "held", "Left out by the manager"))
    elif action == "accept_suggestion":
        if not row["suggestion"]:
            raise ResolutionRejected(["This row has no suggestion to accept"])
        _apply_values(settings, shift, name, row, action, row["suggestion"])
    elif action == "enter_values":
        _apply_values(settings, shift, name, row, action, values)
    else:
        raise ResolutionRejected(["Action must be accept_suggestion, enter_values, or leave_out"])

    shift["report"] = build_report(shift)
    return next(r for r in shift[name] if r["id"] == row_id)


def _apply_values(settings, shift, name, row, action, values):
    editable = set(row["original"]) - {_ID_FIELDS[name]}
    if not isinstance(values, dict) or not values:
        raise ResolutionRejected(["Enter at least one value"])
    unknown = sorted(set(values) - editable)
    if unknown:
        raise ResolutionRejected([f"Can't change: {', '.join(unknown)}"])

    changes = {key: str(value).strip() for key, value in values.items()}
    updated = {**row["original"], **changes}
    checked = _CHECKS[name](settings, updated)
    findings = checked["findings"]
    if any(finding["severity"] in ("held", "rejected") for finding in findings):
        raise ResolutionRejected([f["message"] for f in findings if f["severity"] in ("held", "rejected")])

    carried = [f for f in row["findings"] if f["code"] in _DUPLICATE_CODES]
    summary = ", ".join(f"{key} to {value}" for key, value in changes.items())
    decision = _finding("manager_decision", "corrected", f"Changed by the manager: {summary}")
    resolved = _build_row(row["id"], row["row_number"], row["original"], checked, [*carried, decision])
    resolved["decision"] = {"action": action, "values": changes}
    shift[name][shift[name].index(row)] = resolved


def read_rows(settings, name, data, problems):
    mapping = settings["upload"]["column_mappings"][name]
    try:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = {header.strip() for header in reader.fieldnames or []}
        records = list(reader)
    except (UnicodeDecodeError, csv.Error):
        problems.append({"path": name, "message": "File can't be read"})
        return []

    missing = [(field, column) for field, column in mapping.items() if column not in headers]
    for field, column in missing:
        problems.append({"path": f"{name}.{field}", "message": f"Missing column: {column}"})
    if missing:
        return []

    rows = []
    for number, record in enumerate(records, start=2):
        cleaned = {(key or "").strip(): value for key, value in record.items()}
        original = {field: (cleaned.get(column) or "").strip() for field, column in mapping.items()}
        rows.append((number, original))
    return rows


def _process_rows(settings, name, raw_rows):
    id_field = _ID_FIELDS[name]
    groups = {}
    for number, original in raw_rows:
        groups.setdefault(original[id_field], []).append((number, original))

    rows = []
    for row_id, group in groups.items():
        number, original = group[-1]
        carried = []
        if len(group) > 1:
            if all(other == original for _, other in group):
                carried.append(_finding("duplicate_exact", "corrected", "Exact duplicate removed"))
            else:
                carried.append(
                    _finding("duplicate_conflict", "flagged", "Duplicate ID with different values; used the last row")
                )
        if not row_id:
            checked = _rejected("missing_id", "Row has no ID")
        else:
            checked = _CHECKS[name](settings, original)
        rows.append(_build_row(row_id, number, original, checked, carried))
    return rows


def _build_row(row_id, number, original, checked, carried):
    findings = [*carried, *checked["findings"]]
    result = "accepted"
    for finding in findings:
        if _RESULTS.index(finding["severity"]) > _RESULTS.index(result):
            result = finding["severity"]
    return {
        "id": row_id,
        "row_number": number,
        "original": original,
        "values": checked["values"],
        "findings": findings,
        "result": result,
        "plannable": checked["plannable"] and result not in ("held", "rejected"),
        "suggestion": checked.get("suggestion"),
        "decision": None,
    }


def _rejected(code, message, values=None):
    return {"values": values or {}, "findings": [_finding(code, "rejected", message)], "plannable": False}


def _finding(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def _translate(settings, key, raw):
    for label, target in settings["upload"]["value_mappings"][key].items():
        if label.casefold() == raw.casefold():
            return target
    return raw


def _number(text):
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _positive_int(text):
    value = _number(text)
    return int(value) if value is not None and value > 0 and value == int(value) else None


def _parse_time(text):
    """Return (minutes since midnight, whether it was written with AM or PM), or None."""
    match = _TIME_24.fullmatch(text)
    if match:
        hours, minutes = int(match[1]), int(match[2])
        return (hours * 60 + minutes, False) if hours < 24 and minutes < 60 else None
    match = _TIME_AM_PM.fullmatch(text)
    if match:
        hours, minutes = int(match[1]), int(match[2] or 0)
        if not 1 <= hours <= 12 or minutes >= 60:
            return None
        hours = hours % 12 + (12 if match[3].casefold() == "p" else 0)
        return hours * 60 + minutes, True
    return None


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock(text):
    return _parse_time(text)[0]


def _check_consignment(settings, original):
    upload = settings["upload"]
    values = dict(original)
    marker = original["address_line_1"].casefold()
    if any(marker == test.casefold() for test in upload["test_row_markers"]):
        return _rejected("test_row", "This is a test row, not a real order", values)

    findings = []

    _check_coded(settings, values, "payment_type", PAYMENT_TYPES, "payment type", findings)
    _check_coded(settings, values, "handling", HANDLING_TYPES, "handling", findings)
    pieces = _positive_int(original["pieces"])
    values["pieces"] = pieces
    if pieces is None:
        findings.append(_finding("unknown_pieces", "held", f"Unrecognised pieces: {original['pieces'] or 'blank'}"))
    attempt = _positive_int(original["attempt_number"])
    values["attempt_number"] = attempt
    if attempt is None:
        findings.append(
            _finding("unknown_attempt", "held", f"Unrecognised attempt number: {original['attempt_number'] or 'blank'}")
        )

    suggestion = _check_size(settings, original, values, pieces or 1, findings)

    plannable = _check_cod(settings, original, values, findings)
    _check_slot(settings, original, values, findings)
    if not original["customer_phone"]:
        findings.append(_finding("blank_phone", "flagged", "Phone number is blank, so the customer can't be contacted"))

    return {"values": values, "findings": findings, "plannable": plannable, "suggestion": suggestion}


def _check_coded(settings, values, key, allowed, label, findings):
    raw = values[key]
    translated = _translate(settings, key, raw)
    values[key] = translated
    if translated not in allowed:
        findings.append(_finding(f"unknown_{key}", "held", f"Unrecognised {label}: {raw or 'blank'}"))


def _check_size(settings, original, values, pieces, findings):
    upload = settings["upload"]
    box = upload["default_box_cm"]
    dimensions = [_number(original[key]) for key in ("length_cm", "width_cm", "height_cm")]
    if any(value is None or value <= 0 for value in dimensions):
        dimensions = [box["length"], box["width"], box["height"]]
        size = f"{box['length']} × {box['width']} × {box['height']} cm"
        findings.append(_finding("default_size", "flagged", f"Box size is missing, so we used {size}"))
    values["length_cm"], values["width_cm"], values["height_cm"] = dimensions
    volume = dimensions[0] * dimensions[1] * dimensions[2] / 1_000_000 * pieces

    weight = _number(original["weight_kg"])
    values["weight_kg"] = weight
    if weight is None or weight <= 0:
        shown = original["weight_kg"] or "blank"
        findings.append(_finding("impossible_weight", "held", f"Weight isn't possible: {shown}"))
        weight = 0
    elif weight / volume > upload["max_kg_per_m3"] and weight > upload["weight_unit_limit_kg"]:
        suggested = f"{weight / 1000:g}"
        message = f"Weight looks wrong for the box: {weight:g} kg. Did you mean {suggested} kg?"
        findings.append(_finding("weight_unit", "held", message))
        return {"weight_kg": suggested}

    types = settings["vehicle_types"]
    if weight > max(t["max_weight_kg"] for t in types) or volume > max(t["max_volume_m3"] for t in types):
        message = f"Too big for every vehicle: {weight:g} kg, {volume:.2f} m³"
        findings.append(_finding("too_big", "held", message))
    return None


def _check_cod(settings, original, values, findings):
    amount = _number(original["cod_amount"]) if original["cod_amount"] else None
    values["cod_amount"] = amount
    limit = settings["driver_rules"]["cash_limit"]
    currency = settings["currency"]
    if values["payment_type"] == "COD":
        if amount is None or amount <= 0:
            findings.append(_finding("cod_error", "flagged", "COD order has no amount to collect"))
        elif amount > limit:
            message = f"COD of {currency} {amount:g} is over the {currency} {limit:g} cash limit, so no route can carry it"
            findings.append(_finding("over_cash_limit", "flagged", message))
            return False
    elif values["payment_type"] == "PREPAID" and original["cod_amount"]:
        findings.append(_finding("cod_error", "flagged", f"Prepaid order has a COD amount: {original['cod_amount']}"))
    return True


def _check_slot(settings, original, values, findings):
    raw_start, raw_end = original["slot_start"], original["slot_end"]
    values["slot_start"], values["slot_end"] = None, None
    if not raw_start and not raw_end:
        return

    start, end = _parse_time(raw_start), _parse_time(raw_end)
    if start is None or end is None:
        findings.append(_finding("slot_unreadable", "flagged", "Slot can't be read, so it's planned as any time"))
        return
    if start[1] or end[1]:
        findings.append(_finding("slot_format", "corrected", f"Slot {raw_start}–{raw_end} changed to 24-hour time"))
    start, end = start[0], end[0]
    if start == end:
        findings.append(_finding("slot_unreadable", "flagged", "Slot has no length, so it's planned as any time"))
        return
    if end < start:
        start, end = end, start
        message = f"Slot {raw_start}–{raw_end} was reversed, so it's planned as {_hhmm(start)}–{_hhmm(end)}"
        findings.append(_finding("slot_reversed", "flagged", message))

    day = settings["day"]
    if start < _clock(day["delivery_start"]) or end > _clock(day["delivery_end"]):
        message = f"Slot {_hhmm(start)}–{_hhmm(end)} is outside delivery hours, so it's planned as any time"
        findings.append(_finding("slot_outside_hours", "flagged", message))
        return

    values["slot_start"], values["slot_end"] = _hhmm(start), _hhmm(end)
    offered = {(w["start"], w["end"]) for w in settings["slots"]["windows"]}
    if (values["slot_start"], values["slot_end"]) not in offered:
        message = f"Slot {_hhmm(start)}–{_hhmm(end)} isn't one the client offers"
        findings.append(_finding("slot_not_offered", "flagged", message))


def _check_status(settings, values, key, allowed, findings):
    label = "status"
    raw = values["status"]
    translated = _translate(settings, key, raw)
    values["status"] = translated
    if translated not in allowed:
        findings.append(_finding(f"unknown_{key}", "held", f"Unrecognised {label}: {raw or 'blank'}"))
        return False
    return translated == "AVAILABLE"


def _check_driver(settings, original):
    values = dict(original)
    findings = []
    plannable = _check_status(settings, values, "driver_status", DRIVER_STATUSES, findings)

    raw = original["licence"]
    licence = _translate(settings, "licence", raw)
    values["licence"] = licence
    if licence not in {entry["licence_id"] for entry in settings["licences"]}:
        findings.append(_finding("unknown_licence", "held", f"Unrecognised licence: {raw or 'blank'}"))

    experience = _number(original.get("experience_months", ""))
    values["experience_months"] = experience

    start, end = _parse_time(original["shift_start"]), _parse_time(original["shift_end"])
    if start is None or end is None:
        findings.append(_finding("unreadable_shift", "held", "Shift times can't be read"))
    else:
        plannable = _check_shift(settings, values, start[0], end[0], findings) and plannable
    return {"values": values, "findings": findings, "plannable": plannable}


def _check_shift(settings, values, start, end, findings):
    values["shift_start"], values["shift_end"] = _hhmm(start), _hhmm(end)
    day = settings["day"]
    opens, closes = _clock(day["delivery_start"]), _clock(day["delivery_end"])
    if end <= start or end <= opens or start >= closes:
        message = f"Shift {_hhmm(start)}–{_hhmm(end)} is outside delivery hours, so this driver is excluded"
        findings.append(_finding("shift_outside_hours", "flagged", message))
        return False

    limit = int(settings["driver_rules"]["max_working_hours"] * 60)
    if end - start > limit:
        capped = _hhmm(start + limit)
        values["shift_end"] = capped
        message = f"Shift {_hhmm(start)}–{_hhmm(end)} is over the working limit, so it ends at {capped}"
        findings.append(_finding("shift_capped", "flagged", message))
    return True


_VEHICLE_NUMBERS = {
    "max_weight_kg": "Weight limit",
    "max_volume_m3": "Volume limit",
    "fixed_cost_per_day": "Fixed cost",
    "cost_per_km": "Cost per km",
}


def _check_vehicle(settings, original):
    values = dict(original)
    findings = []
    plannable = _check_status(settings, values, "vehicle_status", VEHICLE_STATUSES, findings)

    raw = original["type"]
    type_id = _translate(settings, "vehicle_type", raw)
    values["type"] = type_id
    vehicle_type = next((t for t in settings["vehicle_types"] if t["type_id"] == type_id), None)
    if vehicle_type is None:
        findings.append(_finding("unknown_vehicle_type", "held", f"Unrecognised vehicle type: {raw or 'blank'}"))
        return {"values": values, "findings": findings, "plannable": False}

    for key, label in _VEHICLE_NUMBERS.items():
        declared = original.get(key, "")
        expected = vehicle_type[key]
        values[key] = expected
        if declared and _number(declared) != expected:
            message = f"{label} of {declared} differs from the {type_id} settings, so we used {expected:g}"
            findings.append(_finding("vehicle_differs", "flagged", message))

    expected = vehicle_type.get("refrigerated", False)
    values["refrigerated"] = expected
    declared = original.get("refrigerated", "")
    if declared and declared.casefold() != str(expected).casefold():
        message = f"Refrigerated says {declared}, but {type_id} is set to {expected}, so we used the settings"
        findings.append(_finding("vehicle_differs", "flagged", message))
    return {"values": values, "findings": findings, "plannable": plannable}


_CHECKS = {
    "consignments": _check_consignment,
    "drivers": _check_driver,
    "vehicles": _check_vehicle,
}
