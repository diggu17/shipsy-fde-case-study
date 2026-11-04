"""Checks any plan against every rule and reports its metrics. It never creates or changes a plan."""

import math
from collections import Counter
from datetime import date

from dispatch_planner.address import distance_m
from dispatch_planner.settings import active_dated_rules, time_to_minutes


BROKEN_PLAN_RULES = {"plan_format", "consignment_count", "unknown_consignment", "empty_route", "no_location"}


def check_plan(settings, shift, assessments, plan):
    """Return whether a plan is valid, every violation, calculated route times, and metrics."""
    context = Context(settings, shift, assessments)
    violations = []
    routes = []
    format_ok = _check_format(context, plan, violations)
    if format_ok:
        usable = _check_routes_format(plan["routes"], violations)
        _check_consignment_counts(context, plan, violations)
        _check_reuse(usable, violations)
        for route in usable:
            result = _check_route(context, plan, route, violations)
            if result:
                routes.append(result)
        _check_unassigned(plan, violations)
    return {
        "valid": not any(v["severity"] == "hard" for v in violations),
        "violations": violations,
        "routes": routes,
        "metrics": _metrics(context, routes, violations),
    }


class Context:
    def __init__(self, settings, shift, assessments):
        self.settings = settings
        self.shift = shift
        self.consignments = {row["id"]: row for row in shift["consignments"]}
        self.drivers = {row["id"]: row for row in shift["drivers"]}
        self.vehicles = {row["id"]: row for row in shift["vehicles"]}
        self.assessments = {a["consignment_id"]: a for a in assessments}
        self.vehicle_types = {t["type_id"]: t for t in settings["vehicle_types"]}
        self.licences = {entry["licence_id"]: entry["vehicle_types"] for entry in settings["licences"]}


def _violation(rule, severity, message, route_id=None, consignment_id=None, cost=0):
    return {
        "rule": rule,
        "severity": severity,
        "message": message,
        "route_id": route_id,
        "consignment_id": consignment_id,
        "cost": cost,
    }


def _hard(rule, message, route_id=None, consignment_id=None):
    return _violation(rule, "hard", message, route_id, consignment_id)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_format(context, plan, violations):
    if not isinstance(plan, dict):
        violations.append(_hard("plan_format", "Plan must be an object"))
        return False
    ok = True
    for key, expected in (
        ("plan_date", context.shift.get("plan_date")),
        ("client_id", context.shift.get("client_id")),
        ("hub_id", context.shift.get("hub_id")),
    ):
        if plan.get(key) != expected:
            violations.append(_hard("plan_format", f"{key} must be {expected}, but the plan has {plan.get(key)}"))
            ok = False
    for key in ("routes", "unassigned"):
        if not isinstance(plan.get(key), list):
            violations.append(_hard("plan_format", f"{key} must be a list"))
            ok = False
    if ok and not isinstance(_hub(context.settings, plan["hub_id"]), dict):
        violations.append(_hard("plan_format", f"Hub {plan['hub_id']} isn't in the client's settings"))
        ok = False
    return ok


def _hub(settings, hub_id):
    return next((hub for hub in settings["hubs"] if hub["hub_id"] == hub_id), None)


def _check_routes_format(routes, violations):
    usable = []
    for index, route in enumerate(routes):
        problem = _route_format_problem(route)
        if problem:
            route_id = route.get("route_id") if isinstance(route, dict) else None
            violations.append(_hard("plan_format", f"Route {route_id or index + 1}: {problem}", route_id))
        else:
            usable.append(route)
    return usable


def _route_format_problem(route):
    if not isinstance(route, dict):
        return "must be an object"
    for key in ("route_id", "driver_id", "vehicle_id", "depart_hub"):
        if not isinstance(route.get(key), str):
            return f"{key} is required"
    if time_to_minutes(route["depart_hub"]) is None:
        return "depart_hub must be a time in HH:MM format"
    stops = route.get("stops")
    if not isinstance(stops, list):
        return "stops must be a list"
    numbers = []
    for stop in stops:
        if not isinstance(stop, dict) or not isinstance(stop.get("consignment_ids"), list) or not stop["consignment_ids"]:
            return "each stop needs consignment_ids"
        if not _is_number(stop.get("seq")) or not _is_number(stop.get("lat")) or not _is_number(stop.get("lng")):
            return "each stop needs seq, lat, and lng"
        numbers.append(stop["seq"])
    if len(numbers) != len(set(numbers)):
        return "each stop needs its own seq"
    return None


def _check_consignment_counts(context, plan, violations):
    appearances = Counter()
    for route in plan["routes"]:
        if isinstance(route, dict) and isinstance(route.get("stops"), list):
            for stop in route["stops"]:
                if isinstance(stop, dict) and isinstance(stop.get("consignment_ids"), list):
                    appearances.update(stop["consignment_ids"])
    for entry in plan["unassigned"]:
        if isinstance(entry, dict):
            appearances[entry.get("consignment_id")] += 1

    for consignment_id, count in appearances.items():
        if consignment_id not in context.consignments:
            violations.append(_hard("unknown_consignment", f"{consignment_id} isn't in this shift", None, consignment_id))
        elif count > 1:
            message = f"{consignment_id} appears {count} times in the plan"
            violations.append(_hard("consignment_count", message, None, consignment_id))
    for consignment_id, row in context.consignments.items():
        if row["plannable"] and appearances[consignment_id] == 0:
            message = f"{consignment_id} isn't on a route or listed as unassigned"
            violations.append(_hard("consignment_count", message, None, consignment_id))


def _check_reuse(routes, violations):
    for key, rule, label in (("driver_id", "driver_reused", "Driver"), ("vehicle_id", "vehicle_reused", "Vehicle")):
        counts = Counter(route[key] for route in routes)
        for value, count in counts.items():
            if count > 1:
                violations.append(_hard(rule, f"{label} {value} is on {count} routes"))


def _check_route(context, plan, route, violations):
    route_id = route["route_id"]
    if not route["stops"]:
        violations.append(_hard("empty_route", f"Route {route_id} has no stops", route_id))
        return None

    driver = context.drivers.get(route["driver_id"])
    vehicle = context.vehicles.get(route["vehicle_id"])
    if driver is None or not driver["plannable"]:
        violations.append(_hard("driver_unavailable", f"Driver {route['driver_id']} isn't available", route_id))
    if vehicle is None or not vehicle["plannable"]:
        violations.append(_hard("vehicle_unavailable", f"Vehicle {route['vehicle_id']} isn't available", route_id))

    known_driver = driver is not None and time_to_minutes(driver["values"]["shift_start"]) is not None
    vehicle_type = context.vehicle_types.get(vehicle["values"]["type"]) if vehicle else None

    stops = sorted(route["stops"], key=lambda stop: stop["seq"])
    _check_stop_consignments(context, stops, route_id, violations)
    if vehicle_type is None or not known_driver:
        return None

    if vehicle_type["type_id"] not in context.licences.get(driver["values"]["licence"], []):
        message = f"{driver['id']} has a {driver['values']['licence']} licence, which doesn't allow {vehicle['id']}"
        violations.append(_hard("licence", message, route_id))
    _check_load(context, stops, vehicle, vehicle_type, route_id, violations)
    return _check_times(context, plan, route, stops, driver, vehicle_type, violations)


def _check_stop_consignments(context, stops, route_id, violations):
    radius = context.settings["stop_time"]["stop_radius_m"]
    for stop in stops:
        for consignment_id in stop["consignment_ids"]:
            row = context.consignments.get(consignment_id)
            if row is None:
                continue
            if row["result"] in ("held", "rejected"):
                message = f"{consignment_id} is {row['result']}, so it can't go on a route"
                violations.append(_hard("not_plannable", message, route_id, consignment_id))
            assessment = context.assessments.get(consignment_id)
            if assessment is None or not assessment["routable"]:
                message = f"{consignment_id} has no location, so it can't go on a route"
                violations.append(_hard("no_location", message, route_id, consignment_id))
            elif distance_m(assessment["lat"], assessment["lng"], stop["lat"], stop["lng"]) > radius:
                message = f"{consignment_id} is more than {radius:g} m from stop {stop['seq']}"
                violations.append(_hard("stop_location", message, route_id, consignment_id))


def _route_rows(context, stops):
    return [
        context.consignments[consignment_id]
        for stop in stops
        for consignment_id in stop["consignment_ids"]
        if consignment_id in context.consignments
    ]


def _check_load(context, stops, vehicle, vehicle_type, route_id, violations):
    rows = _route_rows(context, stops)
    weight = sum(row["values"].get("weight_kg") or 0 for row in rows)
    volume = sum(_volume(row) for row in rows)
    cash = sum(row["values"].get("cod_amount") or 0 for row in rows if row["values"].get("payment_type") == "COD")
    vehicle_id = vehicle["id"]

    if weight > vehicle_type["max_weight_kg"]:
        message = f"{weight:g} kg is over {vehicle_id}'s {vehicle_type['max_weight_kg']:g} kg limit"
        violations.append(_hard("overweight", message, route_id))
    if volume > vehicle_type["max_volume_m3"]:
        message = f"{volume:.2f} m³ is over {vehicle_id}'s {vehicle_type['max_volume_m3']:g} m³ limit"
        violations.append(_hard("over_volume", message, route_id))
    limit = context.settings["driver_rules"]["cash_limit"]
    if cash > limit:
        currency = context.settings["currency"]
        message = f"This route collects {currency} {cash:g}, over the {currency} {limit:g} limit"
        violations.append(_hard("cash_limit", message, route_id))

    heavy_types = [t for t in context.vehicle_types.values() if "required_for_items_over_kg" in t]
    for row in rows:
        _check_item(row, vehicle_id, vehicle_type, heavy_types, route_id, violations)


def _check_item(row, vehicle_id, vehicle_type, heavy_types, route_id, violations):
    values = row["values"]
    consignment_id = row["id"]
    if values.get("handling") == "CHILLED" and not vehicle_type.get("refrigerated", False):
        message = f"{consignment_id} is chilled, but {vehicle_id} isn't refrigerated"
        violations.append(_hard("chilled", message, route_id, consignment_id))

    item_weight = (values.get("weight_kg") or 0) / (values.get("pieces") or 1)
    allowed = [t["type_id"] for t in heavy_types if item_weight > t["required_for_items_over_kg"]]
    if allowed and vehicle_type["type_id"] not in allowed:
        message = f"{consignment_id} has a {item_weight:g} kg item, which needs {' or '.join(allowed)}"
        violations.append(_hard("heavy_item", message, route_id, consignment_id))

    longest = max(values.get(key) or 0 for key in ("length_cm", "width_cm", "height_cm"))
    if "max_item_weight_kg" in vehicle_type and item_weight > vehicle_type["max_item_weight_kg"]:
        message = f"{consignment_id} has a {item_weight:g} kg item, over {vehicle_id}'s item limit"
        violations.append(_hard("item_limit", message, route_id, consignment_id))
    if "max_item_length_cm" in vehicle_type and longest > vehicle_type["max_item_length_cm"]:
        message = f"{consignment_id} has a {longest:g} cm item, over {vehicle_id}'s item limit"
        violations.append(_hard("item_limit", message, route_id, consignment_id))


def _volume(row):
    values = row["values"]
    sides = [values.get(key) or 0 for key in ("length_cm", "width_cm", "height_cm")]
    return sides[0] * sides[1] * sides[2] / 1_000_000 * (values.get("pieces") or 1)


def _clock(minutes):
    whole = round(minutes)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def travel_minutes(settings, type_id, kilometres, start):
    """Time to cover a distance, changing speed when the leg crosses a peak window."""
    travel = settings["travel"]
    factor = travel["vehicle_speed_factors"].get(type_id, 1)
    windows = [(time_to_minutes(w["start"]), time_to_minutes(w["end"])) for w in travel["peak_windows"]]
    edges = sorted({edge for window in windows for edge in window})

    now = start
    remaining = kilometres
    while remaining > 1e-9:
        peak = any(begin <= now < end for begin, end in windows)
        speed = (travel["speed_peak_kmh"] if peak else travel["speed_offpeak_kmh"]) * factor
        next_edge = next((edge for edge in edges if edge > now), None)
        needed = remaining / speed * 60
        if next_edge is None or now + needed <= next_edge:
            now += needed
            remaining = 0
        else:
            remaining -= (next_edge - now) / 60 * speed
            now = next_edge
    return now - start


def road_km(settings, a, b):
    return distance_m(a[0], a[1], b[0], b[1]) / 1000 * settings["travel"]["road_distance_factor"]


def _stop_type(settings, unit):
    words = unit.split()
    first = words[0] if words else ""
    for entry in settings["stop_time"]["stop_type_words"]:
        for word in entry["words"]:
            if first == word or (first.startswith(word) and first[len(word):].isdigit()):
                return entry["stop_type"]
    return None


def service_minutes(context, stop):
    config = context.settings["stop_time"]
    minutes = config["minutes_by_stop_type"]
    rows = [context.consignments[i] for i in stop["consignment_ids"] if i in context.consignments]

    base = 0
    guessed = False
    gated = False
    pieces = 0
    cod = 0
    gated_areas = set(config["gated_areas"])
    for row in rows:
        assessment = context.assessments.get(row["id"]) or {}
        stop_type = _stop_type(context.settings, assessment.get("unit", ""))
        if stop_type is None:
            guessed = True
            stop_type = config["default_stop_type"]
        base = max(base, minutes[stop_type])
        notes = row["values"].get("notes", "").casefold()
        if assessment.get("area") in gated_areas or any(p.casefold() in notes for p in config["gate_note_phrases"]):
            gated = True
        pieces += row["values"].get("pieces") or 1
        cod += row["values"].get("payment_type") == "COD"
    total = (
        base
        + cod * config["cod_extra_minutes"]
        + max(0, pieces - 1) * config["extra_parcel_minutes"]
        + (config["gate_extra_minutes"] if gated else 0)
    )
    return total, guessed


def _active_bans(settings, plan_date, type_id):
    rules = active_dated_rules(settings, date.fromisoformat(plan_date))
    return [
        (time_to_minutes(rule["no_riding_start"]), time_to_minutes(rule["no_riding_end"]))
        for rule in rules
        if rule["vehicle_type"] == type_id
    ]


def _simulate(context, plan, route, stops, shift_start, type_id, take_break):
    """Walk a route in order, returning its times and which stop the break follows."""
    settings = context.settings
    rules = settings["driver_rules"]
    hub = _hub(settings, plan["hub_id"])
    here = (hub["lat"], hub["lng"])
    now = time_to_minutes(route["depart_hub"])
    break_after = None
    distance = 0
    legs = []
    results = []

    for stop in stops:
        target = (stop["lat"], stop["lng"])
        km = road_km(settings, here, target)
        arrive = now + travel_minutes(settings, type_id, km, now)
        legs.append((now, arrive, f"on the way to stop {stop['seq']}"))
        distance += km
        rows = [context.consignments[i] for i in stop["consignment_ids"] if i in context.consignments]
        slot_starts = [time_to_minutes(r["values"]["slot_start"]) for r in rows if r["values"].get("slot_start")]
        begin = max([arrive, *slot_starts])
        service, guessed = service_minutes(context, stop)
        finish = begin + service
        results.append(
            {
                "seq": stop["seq"],
                "consignment_ids": stop["consignment_ids"],
                "lat": stop["lat"],
                "lng": stop["lng"],
                "eta": _clock(arrive),
                "delivery_start": _clock(begin),
                "service_min": service,
                "stop_type_guessed": guessed,
                "_begin": begin,
            }
        )
        if take_break and break_after is None and finish - shift_start >= rules["break_after_hours"] * 60:
            break_after = stop["seq"]
            finish += rules["break_minutes"]
        now = finish
        here = target

    km = road_km(settings, here, (hub["lat"], hub["lng"]))
    back = now + travel_minutes(settings, type_id, km, now)
    legs.append((now, back, "on the way back to the hub"))
    distance += km
    if take_break and break_after is None:
        back += rules["break_minutes"]
    return results, back, distance, break_after, legs


def _check_times(context, plan, route, stops, driver, vehicle_type, violations):
    settings = context.settings
    rules = settings["driver_rules"]
    money = settings["money"]
    route_id = route["route_id"]
    type_id = vehicle_type["type_id"]
    shift_start = time_to_minutes(driver["values"]["shift_start"])
    shift_end = time_to_minutes(driver["values"]["shift_end"])
    depart = time_to_minutes(route["depart_hub"])
    opens = time_to_minutes(settings["day"]["delivery_start"])
    closes = time_to_minutes(settings["day"]["delivery_end"])

    if depart < shift_start or depart < opens:
        message = f"Route departs at {route['depart_hub']}, before {_clock(max(shift_start, opens))}"
        violations.append(_hard("early_departure", message, route_id))

    results, back, distance, _, _ = _simulate(context, plan, route, stops, shift_start, type_id, False)
    needs_break = back - shift_start > rules["break_required_over_hours"] * 60
    break_after = None
    results, back, distance, break_after, legs = _simulate(context, plan, route, stops, shift_start, type_id, needs_break)

    for begin, end in _active_bans(settings, plan["plan_date"], type_id):
        for leg_start, leg_end, label in legs:
            if leg_end > leg_start and leg_start < end and leg_end > begin:
                message = f"Riding {label} during the {_clock(begin)}–{_clock(end)} ban"
                violations.append(_hard("riding_ban", message, route_id))

    late_ids = []
    for stop in results:
        begin = stop.pop("_begin")
        if begin < opens or begin > closes:
            message = f"Stop {stop['seq']} begins at {_clock(begin)}, outside delivery hours"
            violations.append(_hard("delivery_hours", message, route_id))
        for consignment_id in stop["consignment_ids"]:
            row = context.consignments.get(consignment_id)
            slot_end = row["values"].get("slot_end") if row else None
            if slot_end and begin > time_to_minutes(slot_end):
                late_ids.append(consignment_id)
                message = f"{consignment_id} begins at {_clock(begin)}, after its slot ended at {slot_end}"
                violations.append(
                    _violation("late", "soft", message, route_id, consignment_id, money["late_penalty_per_parcel"])
                )

    working = back - shift_start
    if working > rules["max_working_hours"] * 60:
        message = f"Working time is {working / 60:.1f} hours, over the {rules['max_working_hours']:g} hour limit"
        violations.append(_hard("working_time", message, route_id))

    overtime = max(0, back - shift_end)
    overtime_cost = overtime / 60 * rules["overtime_rate_per_hour"]
    if overtime > 0:
        message = f"Returns at {_clock(back)}, {round(overtime)} minutes after the shift ends"
        violations.append(_violation("overtime", "soft", message, route_id, None, round(overtime_cost, 2)))

    fixed = vehicle_type["fixed_cost_per_day"]
    running = distance * vehicle_type["cost_per_km"]
    late_cost = len(late_ids) * money["late_penalty_per_parcel"]
    return {
        "route_id": route_id,
        "driver_id": route["driver_id"],
        "vehicle_id": route["vehicle_id"],
        "depart_hub": route["depart_hub"],
        "return_hub": _clock(back),
        "distance_km": round(distance, 2),
        "working_minutes": round(working),
        "overtime_minutes": round(overtime),
        "break_after_seq": break_after,
        "stops": results,
        "late_consignments": late_ids,
        "cost": {
            "fixed": fixed,
            "distance": round(running, 2),
            "overtime": round(overtime_cost, 2),
            "late": late_cost,
            "total": round(fixed + running + overtime_cost + late_cost, 2),
        },
        "_return": back,
    }


def _check_unassigned(plan, violations):
    for entry in plan["unassigned"]:
        if isinstance(entry, dict):
            consignment_id = entry.get("consignment_id")
            reason = entry.get("reason") or "No reason given"
            violations.append(_violation("unassigned", "soft", f"{consignment_id} is unassigned: {reason}", None, consignment_id))


def _metrics(context, routes, violations):
    returns = [route.pop("_return") for route in routes]
    routed = [c for route in routes for stop in route["stops"] for c in stop["consignment_ids"]]
    late = [c for route in routes for c in route["late_consignments"]]
    total = round(sum(route["cost"]["total"] for route in routes), 2)
    risky = Counter()
    for consignment_id in routed:
        quality = (context.assessments.get(consignment_id) or {}).get("quality")
        if quality and quality != "GOOD":
            risky[quality] += 1
    return {
        "total_cost": total,
        "routed": len(routed),
        "unassigned": sum(v["rule"] == "unassigned" for v in violations),
        "cost_per_routed": round(total / len(routed), 2) if routed else None,
        "on_time": len(routed) - len(late),
        "late": {"count": len(late), "penalty": sum(route["cost"]["late"] for route in routes)},
        "overtime": {
            "minutes": sum(route["overtime_minutes"] for route in routes),
            "cost": round(sum(route["cost"]["overtime"] for route in routes), 2),
        },
        "distance_km": round(sum(route["distance_km"] for route in routes), 2),
        "workload": {
            "earliest_return": _clock(min(returns)) if returns else None,
            "latest_return": _clock(max(returns)) if returns else None,
            "gap_minutes": round(max(returns) - min(returns)) if returns else None,
        },
        "risky_stops": dict(risky),
        "guessed_stop_types": sum(s["stop_type_guessed"] for route in routes for s in route["stops"]),
    }
