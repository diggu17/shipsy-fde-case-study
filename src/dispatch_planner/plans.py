"""Saves each version of a shift's plan in Mongo, with a copy of the settings it used."""

from datetime import datetime, timezone


class PlanNotFound(Exception):
    pass


class PlanStore:
    def __init__(self, db):
        self._plans = db.plans

    def save(self, client_id, hub_id, plan_date, document):
        latest = self._find(client_id, hub_id, plan_date)
        version = latest["version"] + 1 if latest else 1
        saved = {
            "client_id": client_id,
            "hub_id": hub_id,
            "plan_date": plan_date,
            "version": version,
            "status": "draft",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **document,
        }
        self._plans.insert_one(dict(saved))
        return saved

    def latest(self, client_id, hub_id, plan_date):
        document = self._find(client_id, hub_id, plan_date)
        if document is None:
            raise PlanNotFound
        document.pop("_id")
        return document

    def set_status(self, client_id, hub_id, plan_date, version, status, alerts=()):
        self._plans.update_one(
            {"client_id": client_id, "hub_id": hub_id, "plan_date": plan_date, "version": version},
            {"$set": {"status": status, "approved_at": datetime.now(timezone.utc).isoformat(), "approved_with_alerts": list(alerts)}},
        )

    def _find(self, client_id, hub_id, plan_date):
        return self._plans.find_one(
            {"client_id": client_id, "hub_id": hub_id, "plan_date": plan_date},
            sort=[("version", -1)],
        )
