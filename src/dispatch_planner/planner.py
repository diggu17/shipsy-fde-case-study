"""Creates a plan for a shift. The plan checker has the final say on anything made here."""

import time
from datetime import date

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from dispatch_planner.address import distance_m
from dispatch_planner.plan_check import Context, check_plan, road_km, service_minutes, travel_minutes
from dispatch_planner.settings import active_dated_rules, time_to_minutes

_DAY_SECONDS = 24 * 3600
_MAX_REPAIRS = 5000
_MAX_RESCUE_SECONDS = 20
_FIRST_SOLUTION = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
_METAHEURISTIC = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
NOT_FITTED = "No route had room for this stop"


def generate_plan(settings, shift, assessments, solver=None):
    """Create a valid plan: the solver's if it works, otherwise a simple backup."""
    solver = solver or solve_routes
    built = build_stops(settings, shift, assessments)
    pairing = pair_drivers(settings, shift)
    hub = next(h for h in settings["hubs"] if h["hub_id"] == shift["hub_id"])

    method = "solver"
    solver_error = None
    try:
        assigned = solver(settings, built["stops"], pairing["pairs"], hub, shift["plan_date"])
    except Exception as error:
        assigned = None
        solver_error = str(error)
    if not assigned:
        method = "backup"
        solver_error = solver_error or "The solver found no plan"
        assigned = _backup_routes(settings, built["stops"], pairing["pairs"], hub)

    plan = _plan_from_routes(settings, shift, built, pairing["pairs"], assigned, hub)
    plan = _repair(settings, shift, assessments, plan)
    plan = _rescue(settings, shift, assessments, plan, built["stops"], pairing["pairs"], hub)
    plan = _repair(settings, shift, assessments, plan)
    check = check_plan(settings, shift, assessments, plan)
    return {
        "plan": plan,
        "method": method,
        "solver_error": solver_error,
        "pairs": pairing["pairs"],
        "unused_drivers": pairing["unused_drivers"],
        "check": check,
        "failure": estimate_failures(settings, assessments, check),
    }


def estimate_failures(settings, assessments, check):
    """Estimate failed deliveries on routed parcels. This is reported, and doesn't affect routes."""
    rates = settings["planning"]["failure_rates"]
    by_id = {a["consignment_id"]: a for a in assessments}
    routed = [c for route in check["routes"] for stop in route["stops"] for c in stop["consignment_ids"]]
    expected = sum(rates["with_area"] if by_id[c].get("area") else rates["without_area"] for c in routed if c in by_id)
    cost = expected * settings["money"]["failed_attempt_cost"]
    delivered = len(routed) - expected
    total = check["metrics"]["total_cost"] + cost
    return {
        "expected_failed": round(expected, 1),
        "cost": round(cost, 2),
        "cost_per_expected_delivered": round(total / delivered, 2) if delivered > 0 else None,
    }


def _allowed_types(settings, row):
    values = row["values"]
    weight = values.get("weight_kg") or 0
    pieces = values.get("pieces") or 1
    sides = [values.get(key) or 0 for key in ("length_cm", "width_cm", "height_cm")]
    volume = sides[0] * sides[1] * sides[2] / 1_000_000 * pieces
    item_weight = weight / pieces
    longest = max(sides)
    types = settings["vehicle_types"]
    heavy = {t["type_id"] for t in types if "required_for_items_over_kg" in t and item_weight > t["required_for_items_over_kg"]}

    allowed = []
    for vehicle_type in types:
        if values.get("handling") == "CHILLED" and not vehicle_type.get("refrigerated", False):
            continue
        if heavy and vehicle_type["type_id"] not in heavy:
            continue
        if item_weight > vehicle_type.get("max_item_weight_kg", float("inf")):
            continue
        if longest > vehicle_type.get("max_item_length_cm", float("inf")):
            continue
        if weight > vehicle_type["max_weight_kg"] or volume > vehicle_type["max_volume_m3"]:
            continue
        allowed.append(vehicle_type["type_id"])
    return allowed, weight, volume


def build_stops(settings, shift, assessments):
    """Choose which consignments are routed, and group them into stops."""
    context = Context(settings, shift, assessments)
    limit = settings["driver_rules"]["cash_limit"]
    unassigned = []
    groups = {}

    for row in shift["consignments"]:
        reason = _unroutable_reason(context, row)
        if reason:
            unassigned.append({"consignment_id": row["id"], "reason": reason})
            continue
        allowed, weight, volume = _allowed_types(settings, row)
        if not allowed:
            unassigned.append({"consignment_id": row["id"], "reason": "No vehicle type can carry this parcel"})
            continue
        assessment = context.assessments[row["id"]]
        key = (assessment["lat"], assessment["lng"], tuple(allowed))
        groups.setdefault(key, []).append((row, weight, volume))

    stops = []
    for (lat, lng, allowed), members in groups.items():
        by_slot = {}
        for member in members:
            values = member[0]["values"]
            by_slot.setdefault((values.get("slot_start"), values.get("slot_end")), []).append(member)
        no_slot = by_slot.pop((None, None), [])
        if by_slot:
            first = next(iter(by_slot))
            by_slot[first].extend(no_slot)
        else:
            by_slot[(None, None)] = no_slot
        for (start, end), group in by_slot.items():
            stops.extend(_split_stop(settings, context, group, lat, lng, list(allowed), start, end, limit))
    for number, stop in enumerate(stops):
        stop["stop_id"] = f"S{number + 1}"
    return {"stops": stops, "unassigned": unassigned}


def _unroutable_reason(context, row):
    messages = "; ".join(f["message"] for f in row["findings"])
    if row["result"] == "rejected":
        return f"Rejected: {messages}"
    if row["result"] == "held":
        return f"Needs a decision: {messages}"
    if not row["plannable"]:
        return messages or "Can't be planned"
    assessment = context.assessments.get(row["id"])
    if assessment is None or not assessment["routable"]:
        return "Waiting for the customer to confirm the address; moves to tomorrow if there's no reply"
    return None


def _split_stop(settings, context, group, lat, lng, allowed, start, end, limit):
    types = {t["type_id"]: t for t in settings["vehicle_types"]}
    max_weight = max(types[t]["max_weight_kg"] for t in allowed)
    max_volume = max(types[t]["max_volume_m3"] for t in allowed)

    chunks = []
    current, weight, volume, cash = [], 0, 0, 0
    for row, row_weight, row_volume in group:
        row_cash = row["values"].get("cod_amount") or 0 if row["values"].get("payment_type") == "COD" else 0
        if current and (weight + row_weight > max_weight or volume + row_volume > max_volume or cash + row_cash > limit):
            chunks.append((current, weight, volume, cash))
            current, weight, volume, cash = [], 0, 0, 0
        current.append(row["id"])
        weight += row_weight
        volume += row_volume
        cash += row_cash
    chunks.append((current, weight, volume, cash))

    stops = []
    for ids, chunk_weight, chunk_volume, chunk_cash in chunks:
        stop = {"seq": 0, "consignment_ids": ids, "lat": lat, "lng": lng}
        service, _ = service_minutes(context, stop)
        stops.append(
            {
                "consignment_ids": ids,
                "lat": lat,
                "lng": lng,
                "slot_start": start,
                "slot_end": end,
                "weight": chunk_weight,
                "volume": chunk_volume,
                "cash": chunk_cash,
                "service": service,
                "allowed_types": allowed,
            }
        )
    return stops


def _delivery_overlap(settings, values):
    start = time_to_minutes(values["shift_start"])
    end = time_to_minutes(values["shift_end"])
    opens = time_to_minutes(settings["day"]["delivery_start"])
    closes = time_to_minutes(settings["day"]["delivery_end"])
    return max(0, min(end, closes) - max(start, opens))


def pair_drivers(settings, shift):
    """Give each available vehicle one driver, using simple rules a manager can change."""
    licences = {entry["licence_id"]: set(entry["vehicle_types"]) for entry in settings["licences"]}
    types = {t["type_id"]: t for t in settings["vehicle_types"]}
    drivers = [row for row in shift["drivers"] if row["plannable"]]
    vehicles = sorted(
        (row for row in shift["vehicles"] if row["plannable"]),
        key=lambda row: (-types[row["values"]["type"]]["max_weight_kg"], row["id"]),
    )

    def allowed(driver):
        return licences.get(driver["values"]["licence"], set())

    def experience(driver):
        return driver["values"].get("experience_months") or 0

    pairs = []
    free_drivers = list(drivers)
    free_vehicles = list(vehicles)

    def assign(driver, vehicle):
        pairs.append(
            {
                "driver_id": driver["id"],
                "vehicle_id": vehicle["id"],
                "vehicle_type": vehicle["values"]["type"],
                "shift_start": time_to_minutes(driver["values"]["shift_start"]),
                "shift_end": time_to_minutes(driver["values"]["shift_end"]),
            }
        )
        free_drivers.remove(driver)
        free_vehicles.remove(vehicle)

    # Drivers licensed for only one vehicle type can't drive anything else.
    for driver in sorted(free_drivers, key=lambda d: (time_to_minutes(d["values"]["shift_start"]), d["id"])):
        if len(allowed(driver)) == 1:
            vehicle = next((v for v in free_vehicles if v["values"]["type"] in allowed(driver)), None)
            if vehicle:
                assign(driver, vehicle)

    # Refrigerated vehicles need the widest cover, because they serve every slot.
    for vehicle in [v for v in free_vehicles if types[v["values"]["type"]].get("refrigerated")]:
        options = [d for d in free_drivers if vehicle["values"]["type"] in allowed(d)]
        if options:
            assign(max(options, key=lambda d: (_delivery_overlap(settings, d["values"]), experience(d))), vehicle)

    # Late-shift drivers get the larger vehicles, because the evening is the tightest time.
    late_first = sorted(
        free_drivers,
        key=lambda d: (-time_to_minutes(d["values"]["shift_end"]), -experience(d), d["id"]),
    )
    for driver in late_first:
        vehicle = next((v for v in free_vehicles if v["values"]["type"] in allowed(driver)), None)
        if vehicle:
            assign(driver, vehicle)

    return {"pairs": pairs, "unused_drivers": [d["id"] for d in free_drivers], "unused_vehicles": [v["id"] for v in free_vehicles]}


def _average_speed(settings):
    travel = settings["travel"]
    opens = time_to_minutes(settings["day"]["delivery_start"])
    closes = time_to_minutes(settings["day"]["delivery_end"])
    peak = 0
    for window in travel["peak_windows"]:
        peak += max(0, min(closes, time_to_minutes(window["end"])) - max(opens, time_to_minutes(window["start"])))
    day = closes - opens
    return (peak * travel["speed_peak_kmh"] + (day - peak) * travel["speed_offpeak_kmh"]) / day


def solve_routes(settings, stops, pairs, hub, plan_date):
    """Ask OR-Tools which vehicle takes which stops, and in what order."""
    if not stops or not pairs:
        return None
    types = {t["type_id"]: t for t in settings["vehicle_types"]}
    travel = settings["travel"]
    rules = settings["driver_rules"]
    planning = settings["planning"]
    day = settings["day"]
    drivers_by_id = {p["driver_id"]: (p["shift_start"], p["shift_end"]) for p in pairs}
    used_types = sorted({p["vehicle_type"] for p in pairs})

    points = [(hub["lat"], hub["lng"])] + [(s["lat"], s["lng"]) for s in stops]
    service = [0] + [round(s["service"] * 60) for s in stops]
    size = len(points)
    km = [[road_km(settings, a, b) for b in points] for a in points]
    speed = _average_speed(settings)

    manager = pywrapcp.RoutingIndexManager(size, len(pairs), 0)
    routing = pywrapcp.RoutingModel(manager)

    time_callbacks = {}
    for type_id in used_types:
        kmh = speed * travel["vehicle_speed_factors"].get(type_id, 1)
        matrix = [[service[i] + round(km[i][j] / kmh * 3600) for j in range(size)] for i in range(size)]
        time_callbacks[type_id] = routing.RegisterTransitMatrix(matrix)
        cost = [[round(km[i][j] * types[type_id]["cost_per_km"] * 100) for j in range(size)] for i in range(size)]
        cost_callback = routing.RegisterTransitMatrix(cost)
        for number, pair in enumerate(pairs):
            if pair["vehicle_type"] == type_id:
                routing.SetArcCostEvaluatorOfVehicle(cost_callback, number)

    routing.AddDimensionWithVehicleTransits(
        [time_callbacks[p["vehicle_type"]] for p in pairs], _DAY_SECONDS, _DAY_SECONDS, False, "time"
    )
    clock = routing.GetDimensionOrDie("time")

    _add_load(routing, stops, pairs, types, "weight", lambda s: round(s["weight"] * 100), lambda t: round(t["max_weight_kg"] * 100))
    _add_load(routing, stops, pairs, types, "volume", lambda s: round(s["volume"] * 10000), lambda t: round(t["max_volume_m3"] * 10000))
    cash_limit = round(rules["cash_limit"])
    cash_callback = routing.RegisterUnaryTransitVector([0] + [round(s["cash"]) for s in stops])
    routing.AddDimensionWithVehicleCapacity(cash_callback, 0, [cash_limit] * len(pairs), True, "cash")

    opens = time_to_minutes(day["delivery_start"]) * 60
    closes = time_to_minutes(day["delivery_end"]) * 60
    late_per_second = max(1, round(planning["late_weight"] * 100 / 1800))
    overtime_per_second = max(1, round(rules["overtime_rate_per_hour"] * 100 / 3600))

    for number, stop in enumerate(stops, start=1):
        index = manager.NodeToIndex(number)
        clock.CumulVar(index).SetRange(opens, closes)
        if stop["slot_start"]:
            clock.CumulVar(index).SetMin(time_to_minutes(stop["slot_start"]) * 60)
        if stop["slot_end"]:
            clock.SetCumulVarSoftUpperBound(index, time_to_minutes(stop["slot_end"]) * 60, late_per_second)
        penalty = round(planning["unassigned_penalty"] * 100 * len(stop["consignment_ids"]))
        routing.AddDisjunction([index], penalty)
        forbidden = [v for v, p in enumerate(pairs) if p["vehicle_type"] not in stop["allowed_types"]]
        if forbidden:
            routing.VehicleVar(index).RemoveValues(forbidden)

    break_seconds = rules["break_minutes"] * 60
    limit = round(rules["max_working_hours"] * 3600)
    threshold = rules["break_required_over_hours"] * 3600
    active_rules = active_dated_rules(settings, date.fromisoformat(plan_date))
    node_service = [service[manager.IndexToNode(i)] if not routing.IsEnd(i) else 0 for i in range(routing.Size())]
    for number, pair in enumerate(pairs):
        routing.SetFixedCostOfVehicle(round(types[pair["vehicle_type"]]["fixed_cost_per_day"] * 100), number)
        shift_start, shift_end = drivers_by_id[pair["driver_id"]]
        start, end = routing.Start(number), routing.End(number)
        begin = max(shift_start * 60, opens)
        clock.CumulVar(start).SetRange(begin, closes)
        clock.CumulVar(end).SetMax(shift_start * 60 + limit - break_seconds)
        soft_end = shift_end * 60 - (break_seconds if (shift_end - shift_start) * 60 > threshold else 0)
        clock.SetCumulVarSoftUpperBound(end, max(begin, soft_end), overtime_per_second)
        routing.AddVariableMaximizedByFinalizer(clock.CumulVar(start))
        routing.AddVariableMinimizedByFinalizer(clock.CumulVar(end))

        intervals = []
        for rule in active_rules:
            if rule["vehicle_type"] == pair["vehicle_type"]:
                ban_start = time_to_minutes(rule["no_riding_start"]) * 60
                ban_end = time_to_minutes(rule["no_riding_end"]) * 60
                intervals.append(
                    routing.solver().FixedDurationIntervalVar(ban_start, ban_start, ban_end - ban_start, False, f"ban{number}")
                )
        if intervals:
            clock.SetBreakIntervalsOfVehicle(intervals, number, node_service)

    clock.SetGlobalSpanCostCoefficient(round(planning["workload_weight"] * 100 / 3600))

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = _FIRST_SOLUTION
    search.local_search_metaheuristic = _METAHEURISTIC
    search.time_limit.FromSeconds(int(planning["solver_seconds"]))
    solution = routing.SolveWithParameters(search)
    if solution is None:
        return None

    assigned = {}
    for number, pair in enumerate(pairs):
        index = routing.Start(number)
        route = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                route.append(stops[node - 1])
            index = solution.Value(routing.NextVar(index))
        if route:
            assigned[pair["vehicle_id"]] = route
    return assigned


def _add_load(routing, stops, pairs, types, name, stop_load, type_capacity):
    callback = routing.RegisterUnaryTransitVector([0] + [stop_load(s) for s in stops])
    capacities = [type_capacity(types[p["vehicle_type"]]) for p in pairs]
    routing.AddDimensionWithVehicleCapacity(callback, 0, capacities, True, name)


def _backup_routes(settings, stops, pairs, hub):
    """A simple plan: take stops by slot, and add each to the nearest vehicle with room."""
    types = {t["type_id"]: t for t in settings["vehicle_types"]}
    limit = settings["driver_rules"]["cash_limit"]
    state = {
        p["vehicle_id"]: {"pair": p, "stops": [], "weight": 0, "volume": 0, "cash": 0, "at": (hub["lat"], hub["lng"])}
        for p in pairs
    }

    def slot_order(stop):
        return (stop["slot_start"] is None, stop["slot_start"] or "")

    for stop in sorted(stops, key=slot_order):
        best = None
        best_distance = None
        for vehicle_id, route in state.items():
            vehicle_type = types[route["pair"]["vehicle_type"]]
            if route["pair"]["vehicle_type"] not in stop["allowed_types"]:
                continue
            if route["weight"] + stop["weight"] > vehicle_type["max_weight_kg"]:
                continue
            if route["volume"] + stop["volume"] > vehicle_type["max_volume_m3"]:
                continue
            if route["cash"] + stop["cash"] > limit:
                continue
            distance = distance_m(*route["at"], stop["lat"], stop["lng"])
            if best is None or distance < best_distance:
                best, best_distance = vehicle_id, distance
        if best:
            route = state[best]
            route["stops"].append(stop)
            route["weight"] += stop["weight"]
            route["volume"] += stop["volume"]
            route["cash"] += stop["cash"]
            route["at"] = (stop["lat"], stop["lng"])
    return {vehicle_id: route["stops"] for vehicle_id, route in state.items() if route["stops"]}


def _departure(settings, pair, first_stop, hub, shift_start):
    opens = time_to_minutes(settings["day"]["delivery_start"])
    earliest = max(shift_start, opens)
    if not first_stop["slot_start"]:
        return earliest
    slot = time_to_minutes(first_stop["slot_start"])
    km = road_km(settings, (hub["lat"], hub["lng"]), (first_stop["lat"], first_stop["lng"]))
    needed = travel_minutes(settings, pair["vehicle_type"], km, max(earliest, slot - 60))
    return max(earliest, int(slot - needed))


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _plan_from_routes(settings, shift, built, pairs, assigned, hub):
    by_vehicle = {p["vehicle_id"]: p for p in pairs}
    routes = []
    routed = set()
    for vehicle_id in sorted(assigned):
        route_stops = assigned[vehicle_id]
        pair = by_vehicle[vehicle_id]
        depart = _departure(settings, pair, route_stops[0], hub, pair["shift_start"])
        routes.append(
            {
                "route_id": f"R{len(routes) + 1:02d}",
                "driver_id": pair["driver_id"],
                "vehicle_id": vehicle_id,
                "depart_hub": _clock(depart),
                "stops": [
                    {"seq": seq, "consignment_ids": list(stop["consignment_ids"]), "lat": stop["lat"], "lng": stop["lng"]}
                    for seq, stop in enumerate(route_stops, start=1)
                ],
            }
        )
        routed.update(stop["stop_id"] for stop in route_stops)

    unassigned = list(built["unassigned"])
    for stop in built["stops"]:
        if stop["stop_id"] not in routed:
            unassigned.extend({"consignment_id": c, "reason": NOT_FITTED} for c in stop["consignment_ids"])
    return {
        "plan_date": shift["plan_date"],
        "client_id": shift["client_id"],
        "hub_id": shift["hub_id"],
        "assumptions": list(settings["planning"]["assumptions"]),
        "routes": routes,
        "unassigned": unassigned,
    }


def _repair(settings, shift, assessments, plan):
    """Move anything that breaks a hard rule to unassigned, until the plan is valid."""
    for _ in range(_MAX_REPAIRS):
        hard = [v for v in check_plan(settings, shift, assessments, plan)["violations"] if v["severity"] == "hard"]
        if not hard:
            break
        violation = hard[0]
        if violation["consignment_id"] and _remove_consignment(plan, violation["consignment_id"], violation["message"]):
            continue
        if not violation["route_id"] or not _remove_last_stop(plan, violation["route_id"], violation["message"]):
            raise RuntimeError(f"Couldn't repair the plan: {violation['message']}")
    plan["routes"] = [route for route in plan["routes"] if route["stops"]]
    for number, route in enumerate(plan["routes"], start=1):
        route["route_id"] = f"R{number:02d}"
    return plan


def route_score(settings, shift, assessments, plan, route):
    """A route's cost in objective terms, or None if it breaks a hard rule."""
    rows = {row["id"]: row for row in shift["consignments"]}
    ids = {c for stop in route["stops"] for c in stop["consignment_ids"]}
    small_shift = {**shift, "consignments": [rows[c] for c in ids if c in rows]}
    trial = {**plan, "routes": [route], "unassigned": []}
    result = check_plan(settings, small_shift, assessments, trial)
    if any(v["severity"] == "hard" for v in result["violations"]):
        return None
    late = result["metrics"]["late"]["count"]
    return result["metrics"]["total_cost"] + settings["planning"]["late_weight"] * late


def with_stop(stops, entry, position):
    """Return stops with a new one inserted, numbered in order."""
    combined = [*stops[:position], entry, *stops[position:]]
    return [{**stop, "seq": seq} for seq, stop in enumerate(combined, start=1)]


def _rescue(settings, shift, assessments, plan, stops, pairs, hub):
    """Try to fit each left-off stop into a route or an unused vehicle, using the checker's real times."""
    fitted = {e["consignment_id"] for e in plan["unassigned"] if e["reason"] == NOT_FITTED}
    left_off = [s for s in stops if set(s["consignment_ids"]) <= fitted]
    if not left_off:
        return plan

    deadline = time.monotonic() + min(_MAX_RESCUE_SECONDS, settings["planning"]["solver_seconds"])
    types = {p["vehicle_id"]: p for p in pairs}
    used = {route["vehicle_id"] for route in plan["routes"]} | {route["driver_id"] for route in plan["routes"]}
    spare = [p for p in pairs if p["vehicle_id"] not in used and p["driver_id"] not in used]

    def score(route):
        return route_score(settings, shift, assessments, plan, route)

    for stop in sorted(left_off, key=lambda s: (s["slot_start"] is None, s["slot_start"] or "")):
        if time.monotonic() > deadline:
            break
        best = None
        best_extra = None
        entry = {"seq": 0, "consignment_ids": list(stop["consignment_ids"]), "lat": stop["lat"], "lng": stop["lng"]}
        for route in plan["routes"]:
            if types[route["vehicle_id"]]["vehicle_type"] not in stop["allowed_types"]:
                continue
            before = score(route)
            for position in range(len(route["stops"]) + 1):
                trial = {**route, "stops": with_stop(route["stops"], entry, position)}
                after = score(trial)
                if before is not None and after is not None and (best is None or after - before < best_extra):
                    best, best_extra = (route, trial["stops"]), after - before
        for pair in spare:
            if pair["vehicle_type"] in stop["allowed_types"]:
                new_route = {
                    "route_id": f"N{pair['vehicle_id']}",
                    "driver_id": pair["driver_id"],
                    "vehicle_id": pair["vehicle_id"],
                    "depart_hub": _clock(_departure(settings, pair, stop, hub, pair["shift_start"])),
                    "stops": [{**entry, "seq": 1}],
                }
                after = score(new_route)
                if after is not None and (best is None or after < best_extra):
                    best, best_extra = (new_route, new_route["stops"]), after
        if best:
            route, new_stops = best
            route["stops"] = new_stops
            if route not in plan["routes"]:
                plan["routes"].append(route)
                spare = [p for p in spare if p["vehicle_id"] != route["vehicle_id"]]
            ids = set(stop["consignment_ids"])
            plan["unassigned"] = [e for e in plan["unassigned"] if e["consignment_id"] not in ids]
    return plan


def _remove_consignment(plan, consignment_id, reason):
    for route in plan["routes"]:
        for stop in route["stops"]:
            if consignment_id in stop["consignment_ids"]:
                stop["consignment_ids"].remove(consignment_id)
                plan["unassigned"].append({"consignment_id": consignment_id, "reason": reason})
                route["stops"] = [s for s in route["stops"] if s["consignment_ids"]]
                for seq, kept in enumerate(route["stops"], start=1):
                    kept["seq"] = seq
                if not route["stops"]:
                    plan["routes"].remove(route)
                return True
    return False


def _remove_last_stop(plan, route_id, reason):
    route = next((r for r in plan["routes"] if r["route_id"] == route_id), None)
    if route is None:
        return False
    stop = route["stops"].pop()
    plan["unassigned"].extend({"consignment_id": c, "reason": reason} for c in stop["consignment_ids"])
    if not route["stops"]:
        plan["routes"].remove(route)
    return True


def _export_action(assessment):
    """What happens before dispatch, including the customer confirmation when there is one."""
    status = (assessment.get("verification") or {}).get("status")
    if status == "needs_agent":
        return "No phone number, so an agent contacts the shop before dispatch"
    prefix = {
        "whatsapp_triggered": "WhatsApp confirmation triggered (simulated in this prototype). ",
        "answered": "Customer confirmed. ",
    }.get(status, "")
    return prefix + assessment["action"]


def build_export(settings, shift, assessments, plan):
    """Write a plan in the Appendix B format, using the checker's times."""
    check = check_plan(settings, shift, assessments, plan)
    by_id = {a["consignment_id"]: a for a in assessments}
    addresses = []
    for row in shift["consignments"]:
        assessment = by_id.get(row["id"])
        if assessment is None:
            messages = "; ".join(f["message"] for f in row["findings"]) or "Not assessed"
            addresses.append(
                {"consignment_id": row["id"], "lat": None, "lng": None, "confidence": 0, "quality": "INVALID", "action": f"Rejected: {messages}"}
            )
        else:
            addresses.append(
                {
                    "consignment_id": row["id"],
                    "lat": assessment["lat"],
                    "lng": assessment["lng"],
                    "confidence": assessment["confidence"],
                    "quality": assessment["quality"],
                    "action": _export_action(assessment),
                }
            )
    return {
        "plan_date": plan["plan_date"],
        "client_id": plan["client_id"],
        "hub_id": plan["hub_id"],
        "assumptions": plan.get("assumptions", []),
        "routes": [
            {
                "route_id": route["route_id"],
                "driver_id": route["driver_id"],
                "vehicle_id": route["vehicle_id"],
                "depart_hub": route["depart_hub"],
                "return_hub": route["return_hub"],
                "distance_km": route["distance_km"],
                "stops": [
                    {
                        "seq": stop["seq"],
                        "consignment_ids": stop["consignment_ids"],
                        "lat": stop["lat"],
                        "lng": stop["lng"],
                        "eta": stop["eta"],
                        "service_min": stop["service_min"],
                    }
                    for stop in route["stops"]
                ],
            }
            for route in check["routes"]
        ],
        "unassigned": [
            {"consignment_id": entry["consignment_id"], "reason": entry["reason"]} for entry in plan["unassigned"]
        ],
        "addresses": addresses,
    }
