"""Saves and reads client settings in Mongo, checking them every time."""

from pymongo.errors import DuplicateKeyError

from dispatch_planner.settings import check_settings


class InvalidSettings(Exception):
    def __init__(self, problems):
        super().__init__("Settings are invalid")
        self.problems = problems


class StoredSettingsInvalid(InvalidSettings):
    """The saved document no longer passes the checks, such as after a direct Mongo edit."""


class ClientExists(Exception):
    pass


class ClientNotFound(Exception):
    pass


class ClientStore:
    def __init__(self, db):
        self._clients = db.clients

    def create(self, settings):
        _raise_if_invalid(check_settings(settings))
        try:
            self._clients.insert_one({"_id": settings["client_id"], **settings})
        except DuplicateKeyError:
            raise ClientExists from None
        return settings

    def update(self, client_id, settings):
        problems = check_settings(settings)
        if isinstance(settings, dict) and settings.get("client_id") != client_id:
            problems.append(
                {"path": "client_id", "message": "Client ID must match the client being updated"}
            )
        _raise_if_invalid(problems)
        result = self._clients.replace_one({"_id": client_id}, {"_id": client_id, **settings})
        if result.matched_count == 0:
            raise ClientNotFound
        return settings

    def list(self):
        fields = {"client_id": True, "name": True, "hubs.hub_id": True}
        return [
            {"client_id": d["client_id"], "name": d["name"], "hubs": [h["hub_id"] for h in d["hubs"]]}
            for d in self._clients.find({}, fields)
        ]

    def get(self, client_id):
        document = self._clients.find_one({"_id": client_id}, {"_id": False})
        if document is None:
            raise ClientNotFound
        problems = check_settings(document)
        if problems:
            raise StoredSettingsInvalid(problems)
        return document


def _raise_if_invalid(problems):
    if problems:
        raise InvalidSettings(problems)
