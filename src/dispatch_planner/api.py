"""HTTP API for setting up clients."""

import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient

from dispatch_planner.address import (
    assess_shift,
    clean_history,
    confirm_location,
    load_areas,
    reassess_with_area,
    score_accuracy,
)
from dispatch_planner.clients import (
    ClientExists,
    ClientNotFound,
    ClientStore,
    InvalidSettings,
    StoredSettingsInvalid,
)
from dispatch_planner.geocoding import geocoder_for
from dispatch_planner.moves import preview_move
from dispatch_planner.plan_check import BROKEN_PLAN_RULES, check_plan
from dispatch_planner.planner import build_export, estimate_failures, generate_plan
from dispatch_planner.plans import PlanNotFound, PlanStore
from dispatch_planner.replan import replan_for_absent_driver
from dispatch_planner.references import ReferenceStore
from dispatch_planner.settings import time_to_minutes
from dispatch_planner.shifts import ShiftNotFound, ShiftStore
from dispatch_planner.upload import (
    ResolutionRejected,
    RowNotFound,
    RowNotHeld,
    UploadRejected,
    process_shift,
)


def _rejected(status, problems):
    return HTTPException(status_code=status, detail={"problems": problems})


def _message(status, path, message):
    return _rejected(status, [{"path": path, "message": message}])


def create_app(db=None, geocoder=None):
    if db is None:
        uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
        db = MongoClient(uri)[os.environ.get("MONGODB_DATABASE", "dispatch_planner")]

    store = ClientStore(db)
    shifts = ShiftStore(db)
    references = ReferenceStore(db)
    plans = PlanStore(db)
    app = FastAPI(title="Dispatch Planner")

    def client_geocoder(settings):
        return geocoder or geocoder_for(settings, db["geocode_cache"])

    def latest_plan(client_id, hub_id, plan_date):
        try:
            return plans.latest(client_id, hub_id, plan_date)
        except PlanNotFound:
            raise _message(404, "plan", "Plan not found") from None

    def plan_view(saved, shift, assessments):
        """A saved plan plus a fresh check, using the settings it was made with."""
        check = check_plan(saved["settings"], shift, assessments, saved["plan"])
        failure = estimate_failures(saved["settings"], assessments, check)
        settings = {key: saved["settings"][key] for key in ("currency", "hubs")}
        fields = {k: v for k, v in saved.items() if k not in ("settings", "_id")}
        return {**fields, "settings": settings, "check": check, "failure": failure}

    def client_areas(client_id):
        areas = references.areas(client_id)
        if areas is None:
            raise _message(409, "areas", "Upload the client's area list first")
        return areas

    def latest_shift(client_id, hub_id, plan_date):
        try:
            return shifts.latest(client_id, hub_id, plan_date)
        except ShiftNotFound:
            raise _message(404, "plan_date", "Shift upload not found") from None

    def saved_assessments(client_id, hub_id, plan_date):
        assessments = latest_shift(client_id, hub_id, plan_date).get("addresses")
        if assessments is None:
            raise _message(404, "addresses", "Addresses have not been assessed yet")
        return assessments

    def client_settings(client_id):
        try:
            return store.get(client_id)
        except StoredSettingsInvalid as error:
            raise _rejected(409, error.problems) from None
        except ClientNotFound:
            raise _message(404, "client_id", "Client not found") from None

    @app.get("/clients")
    def list_clients():
        return store.list()

    @app.post("/clients", status_code=201)
    def create_client(settings: Any = Body()):
        try:
            return store.create(settings)
        except InvalidSettings as error:
            raise _rejected(422, error.problems) from None
        except ClientExists:
            raise _message(409, "client_id", "Client already exists") from None

    @app.put("/clients/{client_id}")
    def update_client(client_id: str, settings: Any = Body()):
        try:
            return store.update(client_id, settings)
        except InvalidSettings as error:
            raise _rejected(422, error.problems) from None
        except ClientNotFound:
            raise _message(404, "client_id", "Client not found") from None

    @app.get("/clients/{client_id}")
    def read_client(client_id: str):
        return client_settings(client_id)

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/uploads", status_code=201)
    def upload_shift(
        client_id: str,
        hub_id: str,
        plan_date: str,
        consignments: UploadFile = File(),
        drivers: UploadFile = File(),
        vehicles: UploadFile = File(),
    ):
        _check_plan_date(plan_date)
        settings = client_settings(client_id)
        if hub_id not in {hub["hub_id"] for hub in settings["hubs"]}:
            raise _message(404, "hub_id", "Hub not found")
        files = {
            "consignments": consignments.file.read(),
            "drivers": drivers.file.read(),
            "vehicles": vehicles.file.read(),
        }
        try:
            shift = process_shift(settings, files)
        except UploadRejected as error:
            raise _rejected(422, error.problems) from None
        version = shifts.save(client_id, hub_id, plan_date, shift)
        return {"version": version, "report": shift["report"]}

    @app.get("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}")
    def read_shift(client_id: str, hub_id: str, plan_date: str):
        return latest_shift(client_id, hub_id, plan_date)

    @app.post("/clients/{client_id}/areas")
    def upload_areas(client_id: str, file: UploadFile = File()):
        settings = client_settings(client_id)
        try:
            areas = load_areas(settings, file.file.read())
        except UploadRejected as error:
            raise _rejected(422, error.problems) from None
        references.save_areas(client_id, areas)
        return {"areas": len(areas)}

    @app.post("/clients/{client_id}/delivery-history")
    def upload_history(client_id: str, file: UploadFile = File()):
        settings = client_settings(client_id)
        areas = client_areas(client_id)
        try:
            events, report = clean_history(settings, areas, file.file.read())
        except UploadRejected as error:
            raise _rejected(422, error.problems) from None
        references.save_history(client_id, events)
        return report

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/addresses")
    def assess_addresses(client_id: str, hub_id: str, plan_date: str):
        settings = client_settings(client_id)
        shift = latest_shift(client_id, hub_id, plan_date)
        areas = client_areas(client_id)
        assessments = assess_shift(
            settings, shift["consignments"], areas, references.history(client_id), client_geocoder(settings), plan_date
        )
        confirmed = {
            a["consignment_id"]: a
            for a in shift.get("addresses", [])
            if a["source"] == "confirmed" or (a.get("verification") or {}).get("status") == "answered"
        }
        assessments = [confirmed.get(a["consignment_id"], a) for a in assessments]
        shifts.save_addresses(client_id, hub_id, plan_date, assessments)
        return {"summary": _address_summary(assessments), "assessments": assessments}

    @app.get("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/addresses")
    def read_addresses(client_id: str, hub_id: str, plan_date: str):
        assessments = saved_assessments(client_id, hub_id, plan_date)
        return {"summary": _address_summary(assessments), "assessments": assessments}

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/addresses/accuracy")
    def score_addresses(client_id: str, hub_id: str, plan_date: str, file: UploadFile = File()):
        settings = client_settings(client_id)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        try:
            return score_accuracy(settings, assessments, file.file.read())
        except UploadRejected as error:
            raise _rejected(422, error.problems) from None

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/addresses/{consignment_id}/confirm")
    def confirm_address(
        client_id: str,
        hub_id: str,
        plan_date: str,
        consignment_id: str,
        lat: Any = Body(),
        lng: Any = Body(),
    ):
        if not _is_coordinate(lat, 90) or not _is_coordinate(lng, 180):
            raise _message(422, "location", "Location isn't a valid coordinate")
        settings = client_settings(client_id)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        assessment = next((a for a in assessments if a["consignment_id"] == consignment_id), None)
        if assessment is None:
            raise _message(404, "consignment_id", "Row not found")
        confirm_location(settings, assessment, lat, lng)
        shifts.save_addresses(client_id, hub_id, plan_date, assessments)
        return assessment

    @app.get("/clients/{client_id}/areas")
    def read_areas(client_id: str):
        client_settings(client_id)
        return [area["area_name"] for area in client_areas(client_id)]

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/addresses/{consignment_id}/customer-area")
    def record_customer_area(client_id: str, hub_id: str, plan_date: str, consignment_id: str, area_name: str = Body(embed=True)):
        settings = client_settings(client_id)
        areas = client_areas(client_id)
        if area_name not in {area["area_name"] for area in areas}:
            raise _message(422, "area_name", "Area not found")
        shift = latest_shift(client_id, hub_id, plan_date)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        row = next((r for r in shift["consignments"] if r["id"] == consignment_id), None)
        position = next((i for i, a in enumerate(assessments) if a["consignment_id"] == consignment_id), None)
        if row is None or position is None:
            raise _message(404, "consignment_id", "Row not found")
        assessments[position] = reassess_with_area(
            settings, row, areas, references.history(client_id), area_name, client_geocoder(settings), plan_date
        )
        shifts.save_addresses(client_id, hub_id, plan_date, assessments)
        return assessments[position]

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/{file}/{row_id}/resolve")
    def resolve_held_row(
        client_id: str,
        hub_id: str,
        plan_date: str,
        file: str,
        row_id: str,
        action: str = Body(),
        values: Any = Body(default=None),
    ):
        settings = client_settings(client_id)
        try:
            return shifts.resolve(settings, client_id, hub_id, plan_date, file, row_id, action, values)
        except ShiftNotFound:
            raise _message(404, "plan_date", "Shift upload not found") from None
        except RowNotFound:
            raise _message(404, "row_id", "Row not found") from None
        except RowNotHeld:
            raise _message(409, "row_id", "This row isn't held") from None
        except ResolutionRejected as error:
            problems = [{"path": "values", "message": message} for message in error.messages]
            raise _rejected(422, problems) from None

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/check")
    def check_a_plan(client_id: str, hub_id: str, plan_date: str, plan: Any = Body()):
        settings = client_settings(client_id)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        return check_plan(settings, shift, assessments, plan)

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans", status_code=201)
    def create_plan(client_id: str, hub_id: str, plan_date: str):
        settings = client_settings(client_id)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        result = generate_plan(settings, shift, assessments)
        saved = plans.save(
            client_id,
            hub_id,
            plan_date,
            {
                "source": "generated",
                "plan": result["plan"],
                "method": result["method"],
                "solver_error": result["solver_error"],
                "pairs": result["pairs"],
                "unused_drivers": result["unused_drivers"],
                "settings": settings,
            },
        )
        return plan_view(saved, shift, assessments)

    @app.get("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/latest")
    def read_plan(client_id: str, hub_id: str, plan_date: str):
        saved = latest_plan(client_id, hub_id, plan_date)
        return plan_view(saved, latest_shift(client_id, hub_id, plan_date), saved_assessments(client_id, hub_id, plan_date))

    @app.put("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/latest")
    def edit_plan(client_id: str, hub_id: str, plan_date: str, plan: Any = Body()):
        current = latest_plan(client_id, hub_id, plan_date)
        fields = {k: current[k] for k in ("method", "solver_error", "pairs", "unused_drivers", "settings")}
        saved = plans.save(client_id, hub_id, plan_date, {**fields, "source": "edited", "plan": plan})
        return plan_view(saved, latest_shift(client_id, hub_id, plan_date), saved_assessments(client_id, hub_id, plan_date))

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/latest/approve")
    def approve_plan(client_id: str, hub_id: str, plan_date: str):
        current = latest_plan(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        check = check_plan(current["settings"], shift, assessments, current["plan"])
        hard = [v for v in check["violations"] if v["severity"] == "hard"]
        broken = [{"path": v["rule"], "message": v["message"]} for v in hard if v["rule"] in BROKEN_PLAN_RULES]
        if broken:
            raise _rejected(409, broken)
        alerts = [v["message"] for v in hard]
        plans.set_status(client_id, hub_id, plan_date, current["version"], "approved", alerts)
        return plan_view({**current, "status": "approved", "approved_with_alerts": alerts}, shift, assessments)

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/latest/preview-move")
    def preview_a_move(
        client_id: str,
        hub_id: str,
        plan_date: str,
        plan: Any = Body(),
        consignment_id: str = Body(),
        route_id: str | None = Body(default=None),
    ):
        current = latest_plan(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        try:
            return preview_move(current["settings"], shift, assessments, plan, consignment_id, route_id)
        except ValueError as error:
            raise _message(422, "consignment_id", str(error)) from None

    @app.get("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/latest/export")
    def export_plan(client_id: str, hub_id: str, plan_date: str):
        current = latest_plan(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        export = build_export(current["settings"], shift, assessments, current["plan"])
        if current.get("approved_with_alerts"):
            export["approved_with_alerts"] = current["approved_with_alerts"]
        return export

    @app.post("/clients/{client_id}/hubs/{hub_id}/shifts/{plan_date}/plans/replan")
    def replan(client_id: str, hub_id: str, plan_date: str, driver_id: str = Body(), time: str = Body()):
        if time_to_minutes(time) is None:
            raise _message(422, "time", "Time must be in HH:MM format")
        current = latest_plan(client_id, hub_id, plan_date)
        shift = latest_shift(client_id, hub_id, plan_date)
        assessments = saved_assessments(client_id, hub_id, plan_date)
        try:
            return replan_for_absent_driver(
                current["settings"], shift, assessments, current["plan"], driver_id, time, current["unused_drivers"]
            )
        except ValueError as error:
            raise _message(422, "driver_id", str(error)) from None

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="screen")
    return app


def _is_coordinate(value, limit):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= limit


def _address_summary(assessments):
    by_quality = Counter(a["quality"] for a in assessments)
    by_owner = Counter(a["action_owner"] or "none" for a in assessments)
    return {
        "assessed": len(assessments),
        "by_quality": dict(by_quality),
        "by_action_owner": dict(by_owner),
        "not_routable": sum(not a["routable"] for a in assessments),
        "by_geocoding": dict(Counter(a.get("geocoding", "not_needed") for a in assessments)),
        "by_verification": dict(Counter((a.get("verification") or {}).get("status", "none") for a in assessments)),
    }


def _check_plan_date(plan_date):
    try:
        datetime.strptime(plan_date, "%Y-%m-%d")
    except ValueError:
        raise _message(422, "plan_date", "Plan date must be a real date in YYYY-MM-DD format") from None
