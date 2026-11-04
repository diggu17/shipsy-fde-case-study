"""Saves each client's area list and cleaned delivery history in Mongo."""


class ReferenceStore:
    def __init__(self, db):
        self._areas = db.client_areas
        self._history = db.delivery_history

    def save_areas(self, client_id, areas):
        self._areas.replace_one({"_id": client_id}, {"_id": client_id, "areas": areas}, upsert=True)

    def areas(self, client_id):
        document = self._areas.find_one({"_id": client_id})
        return document["areas"] if document else None

    def save_history(self, client_id, events):
        self._history.delete_many({"client_id": client_id})
        if events:
            self._history.insert_many([{"client_id": client_id, **event} for event in events])

    def history(self, client_id):
        return list(self._history.find({"client_id": client_id}, {"_id": False, "client_id": False}))
