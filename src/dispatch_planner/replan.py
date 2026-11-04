"""Proposes a small change to a plan when a driver can't work. The manager decides whether to accept it."""

import copy

from dispatch_planner.plan_check import check_plan
from dispatch_planner.planner import route_score, with_stop
from dispatch_planner.settings import time_to_minutes


def replan_for_absent_driver(settings, shift, assessments, plan, driver_id, now, unused_drivers):
    """Return a proposed plan. Finished stops and routes already on the road never change."""
    proposal = copy.deepcopy(plan)
    route = next((r for r in proposal["routes"] if r["driver_id"] == driver_id), None)
    if route is None:
        raise ValueError(f"Driver {driver_id} has no route in this plan")

    now_minutes = time_to_minutes(now)
    before = check_plan(settings, shift, assessments, plan)
    times = next(r for r in before["routes"] if r["driver_id"] == driver_id)
    dispatched = time_to_minutes(route["depart_hub"]) <= now_minutes

    finished = {
        stop["seq"]
        for stop in times["stops"]
        if dispatched and time_to_minutes(stop["delivery_start"]) + stop["service_min"] <= now_minutes
    }
    completed_stops = [s for s in route["stops"] if s["seq"] in finished]
    remaining = [s for s in route["stops"] if s["seq"] not in finished]
    completed = [c for stop in completed_stops for c in stop["consignment_ids"]]
    changes = []

    if completed_stops:
        route["stops"] = completed_stops
        changes.append(f"{driver_id} keeps {len(completed)} finished parcels and returns to the hub")
    else:
        proposal["routes"].remove(route)
        changes.append(f"{driver_id}'s route is removed")

    remaining = _try_replacement(settings, shift, assessments, proposal, route, remaining, now_minutes, dispatched, unused_drivers, changes)
    remaining = _try_later_routes(settings, shift, assessments, proposal, remaining, now_minutes, changes)
    for stop in remaining:
        reason = f"{driver_id} is absent, and no route could take this parcel today"
        proposal["unassigned"].extend({"consignment_id": c, "reason": reason} for c in stop["consignment_ids"])
        changes.append(f"{', '.join(stop['consignment_ids'])} moved to unassigned")

    after = check_plan(settings, shift, assessments, proposal)
    return {
        "plan": proposal,
        "changes": changes,
        "completed": completed,
        "cost_change": round(after["metrics"]["total_cost"] - before["metrics"]["total_cost"], 2),
        "routed": {"before": before["metrics"]["routed"], "after": after["metrics"]["routed"]},
        "check": after,
    }


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _try_replacement(settings, shift, assessments, proposal, old_route, remaining, now, dispatched, unused_drivers, changes):
    """Give the remaining stops to a spare driver, keeping as many of them as the rules allow."""
    if not remaining:
        return remaining
    used_vehicles = {r["vehicle_id"] for r in proposal["routes"]}
    used_drivers = {r["driver_id"] for r in proposal["routes"]}
    free_vehicles = [v for v in shift["vehicles"] if v["plannable"] and v["id"] not in used_vehicles]
    free_vehicles.sort(key=lambda v: v["id"] != old_route["vehicle_id"])
    licences = {entry["licence_id"]: entry["vehicle_types"] for entry in settings["licences"]}
    spare_drivers = [
        d for d in shift["drivers"] if d["plannable"] and d["id"] in unused_drivers and d["id"] not in used_drivers
    ]
    handover = settings["planning"]["handover_minutes"] if dispatched else 0

    best = None
    for driver in spare_drivers:
        for vehicle in free_vehicles:
            if vehicle["values"]["type"] not in licences.get(driver["values"]["licence"], []):
                continue
            earliest = max(
                now + handover,
                time_to_minutes(driver["values"]["shift_start"]),
                time_to_minutes(settings["day"]["delivery_start"]),
            )
            if not dispatched:
                earliest = max(earliest, time_to_minutes(old_route["depart_hub"]))
            route = {
                "route_id": f"{old_route['route_id']}B",
                "driver_id": driver["id"],
                "vehicle_id": vehicle["id"],
                "depart_hub": _clock(earliest),
                "stops": [],
            }
            kept, left = _fill_route(settings, shift, assessments, proposal, route, remaining)
            if kept and (best is None or len(kept) > len(best[1])):
                best = (route, kept, left)
    if best is None:
        return remaining

    route, kept, left = best
    route["stops"] = [{**stop, "seq": seq} for seq, stop in enumerate(kept, start=1)]
    proposal["routes"].append(route)
    changes.append(
        f"Spare driver {route['driver_id']} takes {len(kept)} stops on {route['vehicle_id']}, leaving at {route['depart_hub']}"
    )
    return left


def _fill_route(settings, shift, assessments, proposal, route, stops):
    kept = []
    left = []
    for stop in stops:
        trial = {**route, "stops": [{**s, "seq": seq} for seq, s in enumerate([*kept, stop], start=1)]}
        if route_score(settings, shift, assessments, proposal, trial) is None:
            left.append(stop)
        else:
            kept.append(stop)
    return kept, left


def _try_later_routes(settings, shift, assessments, proposal, remaining, now, changes):
    """Add stops to routes that haven't left the hub yet, so nobody on the road is disturbed."""
    left = []
    for stop in remaining:
        best = None
        best_extra = None
        for route in proposal["routes"]:
            if time_to_minutes(route["depart_hub"]) <= now:
                continue
            base = route_score(settings, shift, assessments, proposal, route)
            if base is None:
                continue
            for position in range(len(route["stops"]) + 1):
                trial_stops = with_stop(route["stops"], stop, position)
                score = route_score(settings, shift, assessments, proposal, {**route, "stops": trial_stops})
                if score is not None and (best is None or score - base < best_extra):
                    best, best_extra = (route, trial_stops), score - base
        if best:
            best[0]["stops"] = best[1]
            changes.append(f"{', '.join(stop['consignment_ids'])} added to {best[0]['route_id']}, which hasn't left yet")
        else:
            left.append(stop)
    return left
