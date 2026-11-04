"""Works out what moving one parcel would lead to. It never saves anything, and never refuses a limit."""

import copy

from dispatch_planner.plan_check import check_plan

_MEASURED_RULES = {"working_time", "overtime", "cash_limit", "overweight", "over_volume"}
MOVED_OFF_REASON = "Moved off the route by the manager"


def preview_move(settings, shift, assessments, plan, consignment_id, route_id):
    """Return the plan with this parcel moved, its check, new alerts, and before and after values."""
    target = next((r for r in plan["routes"] if r["route_id"] == route_id), None)
    if route_id is not None and target is None:
        raise ValueError("Route not found")
    assessment = next((a for a in assessments if a["consignment_id"] == consignment_id), None)
    if assessment is None:
        raise ValueError("Parcel not found")

    before = check_plan(settings, shift, assessments, plan)
    without = _without_parcel(plan, consignment_id, route_id)
    if route_id is None:
        without["unassigned"].append({"consignment_id": consignment_id, "reason": MOVED_OFF_REASON})
        proposal, position = without, None
    else:
        if not assessment["routable"]:
            raise ValueError(f"{consignment_id} has no location, so a driver has nowhere to go")
        proposal, position = _best_position(settings, shift, assessments, without, assessment, route_id)

    after = check_plan(settings, shift, assessments, proposal)
    return {
        "plan": proposal,
        "check": after,
        "position": position,
        "alerts": _new_alerts(before, after),
        "before": _values(before, route_id),
        "after": _values(after, route_id),
    }


def _without_parcel(plan, consignment_id, route_id):
    copied = copy.deepcopy(plan)
    if any(entry["consignment_id"] == consignment_id for entry in copied["unassigned"]):
        copied["unassigned"] = [e for e in copied["unassigned"] if e["consignment_id"] != consignment_id]
        return copied
    for route in copied["routes"]:
        for stop in route["stops"]:
            if consignment_id in stop["consignment_ids"]:
                if route["route_id"] == route_id:
                    raise ValueError(f"{consignment_id} is already with this driver")
                stop["consignment_ids"].remove(consignment_id)
                route["stops"] = [s for s in route["stops"] if s["consignment_ids"]]
                _renumber(route)
                copied["routes"] = [r for r in copied["routes"] if r["stops"]]
                return copied
    raise ValueError("Parcel not found on this plan")


def _best_position(settings, shift, assessments, plan, assessment, route_id):
    best = None
    size = len(next(r for r in plan["routes"] if r["route_id"] == route_id)["stops"])
    for position in range(size + 1):
        trial = copy.deepcopy(plan)
        route = next(r for r in trial["routes"] if r["route_id"] == route_id)
        stop = {"seq": 0, "consignment_ids": [assessment["consignment_id"]], "lat": assessment["lat"], "lng": assessment["lng"]}
        route["stops"].insert(position, stop)
        _renumber(route)
        check = check_plan(settings, shift, assessments, trial)
        limits_broken = sum(v["severity"] == "hard" for v in check["violations"])
        late = check["metrics"]["late"]["count"]
        score = (limits_broken, check["metrics"]["total_cost"] + settings["planning"]["late_weight"] * late)
        if best is None or score < best[0]:
            best = (score, trial, position)
    return best[1], best[2]


def _renumber(route):
    for seq, stop in enumerate(route["stops"], start=1):
        stop["seq"] = seq


def _new_alerts(before, after):
    """Alerts this move adds, or makes worse. Moving a parcel off a plan isn't itself an alert."""
    known = {(v["rule"], v.get("route_id"), v.get("consignment_id")): v["message"] for v in before["violations"]}
    alerts = []
    for violation in after["violations"]:
        if violation["rule"] == "unassigned":
            continue
        key = (violation["rule"], violation.get("route_id"), violation.get("consignment_id"))
        worse = violation["rule"] in _MEASURED_RULES and known.get(key) != violation["message"]
        if key not in known or worse:
            alerts.append({"rule": violation["rule"], "severity": violation["severity"], "message": violation["message"]})
    return alerts


def _values(check, route_id):
    metrics = check["metrics"]
    values = {"total_cost": metrics["total_cost"], "late": metrics["late"]["count"], "unassigned": metrics["unassigned"]}
    route = next((r for r in check["routes"] if r["route_id"] == route_id), None)
    if route:
        values.update(
            working_minutes=route["working_minutes"],
            return_hub=route["return_hub"],
            overtime_minutes=route["overtime_minutes"],
            parcels=sum(len(stop["consignment_ids"]) for stop in route["stops"]),
        )
    return values
