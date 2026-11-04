import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from dispatch_planner.api import create_app
from dispatch_planner.geocoding import NoMapService

ROOT = Path(__file__).parent.parent
SEEDS = ROOT / "seeds"
SAMPLE_DATA = ROOT / "case_data"
FIXTURES = Path(__file__).parent / "fixtures"
TEST_DATABASE = "dispatch_planner_test"


def load_seed(name):
    return json.loads((SEEDS / f"{name}.json").read_text())


def read_files(folder):
    return {name: (folder / f"{name}.csv").read_bytes() for name in ("consignments", "drivers", "vehicles")}


def sample_files():
    return read_files(SAMPLE_DATA)


def riyadh_files():
    return read_files(FIXTURES / "riyadh")


@pytest.fixture
def db():
    client = MongoClient("mongodb://localhost:27017")
    client.drop_database(TEST_DATABASE)
    yield client[TEST_DATABASE]
    client.drop_database(TEST_DATABASE)
    client.close()


@pytest.fixture
def api(db):
    return TestClient(create_app(db, geocoder=NoMapService()))


@pytest.fixture
def sahra():
    return copy.deepcopy(load_seed("sahra-express"))
