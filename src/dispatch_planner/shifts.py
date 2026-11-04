"""Saves each shift upload as a new version in Mongo."""

from datetime import datetime, timezone

from dispatch_planner.upload import FILES, build_report, resolve_row


class ShiftNotFound(Exception):
    pass


class ShiftStore:
    def __init__(self, db):
        self._uploads = db.shift_uploads

    def save(self, client_id, hub_id, plan_date, shift):
        latest = self._latest_document(client_id, hub_id, plan_date)
        version = latest["version"] + 1 if latest else 1
        self._uploads.insert_one(
            {
                "client_id": client_id,
                "hub_id": hub_id,
                "plan_date": plan_date,
                "version": version,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                **shift,
            }
        )
        return version

    def latest(self, client_id, hub_id, plan_date):
        document = self._latest_document(client_id, hub_id, plan_date)
        if document is None:
            raise ShiftNotFound
        document.pop("_id")
        return document

    def resolve(self, settings, client_id, hub_id, plan_date, name, row_id, action, values):
        document = self._latest_document(client_id, hub_id, plan_date)
        if document is None:
            raise ShiftNotFound
        row = resolve_row(settings, document, name, row_id, action, values)
        self._uploads.update_one(
            {"_id": document["_id"]},
            {"$set": {**{file: document[file] for file in FILES}, "report": build_report(document)}},
        )
        return row

    def save_addresses(self, client_id, hub_id, plan_date, assessments):
        document = self._latest_document(client_id, hub_id, plan_date)
        if document is None:
            raise ShiftNotFound
        self._uploads.update_one({"_id": document["_id"]}, {"$set": {"addresses": assessments}})

    def _latest_document(self, client_id, hub_id, plan_date):
        return self._uploads.find_one(
            {"client_id": client_id, "hub_id": hub_id, "plan_date": plan_date},
            sort=[("version", -1)],
        )
