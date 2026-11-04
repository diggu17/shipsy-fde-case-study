"""Drives the manager screen in a real browser, the way a hub manager would."""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

from conftest import SAMPLE_DATA, load_seed

playwright_api = pytest.importorskip("playwright.sync_api")
DATABASE = "dispatch_planner_screen_test"
SHOTS = os.environ.get("SCREENSHOT_DIR")


@pytest.fixture(scope="module")
def server():
    mongo = MongoClient("mongodb://localhost:27017")
    mongo.drop_database(DATABASE)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dispatch_planner.api:create_app", "--factory", "--port", str(port), "--log-level", "warning"],
        env={**os.environ, "MONGODB_DATABASE": DATABASE, "DISPATCH_GEOCODER": "none"},
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            httpx.get(f"{base}/clients")
            break
        except httpx.TransportError:
            time.sleep(0.25)
    settings = load_seed("sahra-express")
    settings["planning"]["solver_seconds"] = 4
    assert httpx.post(f"{base}/clients", json=settings).status_code == 201
    yield base
    process.terminate()
    process.wait()
    mongo.drop_database(DATABASE)
    mongo.close()


@pytest.fixture(scope="module")
def page(server):
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error:
            pytest.skip("Chrome isn't installed")
        context = browser.new_context(viewport={"width": 1500, "height": 1000}, accept_downloads=True)
        tab = context.new_page()
        tab.errors = []
        tab.on("pageerror", lambda error: tab.errors.append(str(error)))
        tab.route("**/router.project-osrm.org/**", lambda route: route.fulfill(json=tab.road_answer) if tab.road_answer else route.abort())
        tab.road_answer = {"routes": [{"geometry": {"coordinates": [[55.24, 25.12], [55.25, 25.15], [55.24, 25.12]]}}]}
        tab.goto(server)
        yield tab
        browser.close()


def shot(page, name):
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


def see(page, text, timeout=30000):
    playwright_api.expect(page.get_by_text(text, exact=False).first).to_be_visible(timeout=timeout)


def test_the_whole_morning_works_in_a_browser(page):
    # Check today's files.
    page.set_input_files('input[name="consignments"]', SAMPLE_DATA / "consignments.csv")
    page.set_input_files('input[name="drivers"]', SAMPLE_DATA / "drivers.csv")
    page.set_input_files('input[name="vehicles"]', SAMPLE_DATA / "vehicles.csv")
    page.get_by_role("button", name="Check these files").click()
    see(page, "What we found")
    see(page, "Needs your decision (12)")
    shot(page, "1-files")

    # Decide on a held row.
    page.get_by_role("button", name="Use 2.87 kg").click()
    see(page, "Needs your decision (11)")

    # A box size needs three numbers, entered together.
    row = page.locator(".decision", has_text="C0303")
    row.get_by_text("Enter the right value").click()
    row.locator("[data-value]").fill("60x40x40")
    row.get_by_role("button", name="Save value").click()
    see(page, "Needs your decision (10)")

    # Upload the area list and history.
    page.get_by_text("Area list and delivery history").click()
    page.set_input_files('input[name="areas"]', SAMPLE_DATA / "areas.csv")
    page.set_input_files('input[name="history"]', SAMPLE_DATA / "delivery_history.csv")
    page.get_by_role("button", name="Upload these files").click()
    see(page, "5408 of 5856 history rows kept")

    # Check addresses, and score accuracy.
    page.get_by_role("button", name="2. Check addresses").click()
    page.locator("#assess").click()
    see(page, "WhatsApp confirmations")
    see(page, "WhatsApp triggered")
    see(page, "Simulated, not sent")
    shot(page, "2-addresses-top")

    # Finding a parcel, recording the customer's answer, and seeing it move to Answered.
    page.get_by_role("tab", name="No location").click()
    page.locator("#confirm-search").fill("C0062")
    assert page.locator(".confirm-row").count() == 1
    see(page, "Which area is Al Manal Residence in?")
    page.locator("[data-area-for]").select_option("Al Barsha 1")
    page.get_by_role("button", name="Save answer").click()
    see(page, "C0062 updated")
    page.locator("#confirm-search").fill("")
    page.get_by_role("tab", name="Answered").click()
    see(page, "Area: Al Barsha 1")

    page.get_by_text("How accurate are these locations?").click()
    page.set_input_files('input[name="file"]', SAMPLE_DATA / "ground_truth_sample.csv")
    page.get_by_role("button", name="Score accuracy").click()
    see(page, "Our address check")
    shot(page, "2-addresses")

    # Create the plan.
    page.get_by_role("button", name="3. Review and approve the plan").click()
    page.locator("#generate").click()
    see(page, "Plan created", timeout=120000)
    see(page, "No limits broken")
    assert page.locator(".board-row:not(.head)").count() >= 15
    page.wait_for_timeout(1500)
    shot(page, "3-plan")

    # The plan opens on every driver, with no deliveries listed and no route line.
    all_stops = page.locator("#map .leaflet-interactive").count()
    assert all_stops > 100
    assert page.locator(".delivery").count() == 0

    # Choosing a driver shows only that driver's route and deliveries.
    driver = page.locator(".driver-row[data-route]").first
    driver.click()
    see(page, "The line follows the roads")
    deliveries = page.locator(".delivery").count()
    assert deliveries > 0
    assert page.locator("#map .pin:not(.hub)").count() == deliveries
    # Each stop, the hub, and the two strokes of its line: nothing from any other route.
    assert page.locator("#map .leaflet-interactive").count() == deliveries + 3
    page.wait_for_timeout(500)
    shot(page, "3b-one-driver")

    # Choosing a delivery highlights its stop on the map.
    page.locator("[data-stop]").first.click()
    assert page.locator("#map .pin.focused").count() == 1

    # With no road directions, the line is labelled as approximate.
    page.road_answer = None
    page.locator(".driver-row[data-route]").nth(1).click()
    see(page, "Approximate line")
    page.road_answer = {"routes": [{"geometry": {"coordinates": [[55.24, 25.12], [55.25, 25.15], [55.24, 25.12]]}}]}

    # All drivers brings every stop back.
    page.locator(".driver-row[data-all-drivers]").click()
    assert page.locator("#map .leaflet-interactive").count() == all_stops
    assert page.locator(".delivery").count() == 0

    # An edit that breaks a rule blocks approval, with the reason shown.
    driver.click()
    page.get_by_text("Change driver, vehicle, or departure").click()
    page.locator("#route-driver").select_option("D01")
    see(page, "Driver D01 isn't available")
    see(page, "You can still approve this plan")
    shot(page, "4-edit-breaks-a-rule")

    # Approving shows the alerts first, and the manager can cancel.
    page.locator("#approve").click()
    see(page, "Approve with alerts?")
    see(page, "Approve anyway")
    page.locator("#decide-no").click()
    see(page, "Unsaved changes")

    # Undo brings the plan back.
    page.get_by_role("button", name="Undo changes").click()
    see(page, "No limits broken")

    # Move a stop off its route, and save.
    page.locator("[data-stop]").first.click()
    page.locator("[data-reassign]").first.select_option("__none")
    see(page, "Unsaved changes")
    page.get_by_role("tab", name="Unassigned").click()
    see(page, "Moved off the route by the manager")
    page.get_by_role("button", name="Save changes").click()
    see(page, "version 2")

    # Adding a parcel back from the unassigned list is checked straight away, and can be undone.
    page.get_by_role("tab", name="Unassigned").click()
    page.locator("[data-add]").first.select_option(index=1)
    page.wait_for_function("document.getElementById('decide').open || document.body.textContent.includes('Unsaved changes')")
    if page.locator("#decide").evaluate("d => d.open"):
        see(page, "Doing this will lead to")
        shot(page, "4b-move-alert")
        page.locator("#decide-yes").click()
    see(page, "Unsaved changes")
    page.get_by_role("button", name="Undo changes").click()

    # Approve the saved plan.
    page.locator("#approve").click()
    see(page, "Plan approved.")
    see(page, "Approved, version 2")
    assert page.locator("#approve").is_disabled()

    # Export the approved plan.
    page.locator("#more-menu summary").click()
    with page.expect_download() as download:
        page.get_by_role("button", name="Export the plan").click()
    export = json.loads(Path(download.value.path()).read_text())
    assert {"plan_date", "client_id", "hub_id", "assumptions", "routes", "unassigned", "addresses"} <= set(export)
    assert download.value.suggested_filename == "plan-sahra-express-AQ3-2026-10-14.json"

    # A driver can't work.
    page.locator("#more-menu summary").click()
    page.get_by_role("button", name="A driver can't work").click()
    page.locator("#absent-propose").click()
    see(page, "Proposed change")
    see(page, "The plan is valid")
    shot(page, "5-driver-absent")
    page.locator("#absent-accept").click()
    see(page, "Change accepted")
    see(page, "version 3")

    # Client setup rejects bad settings and lists the problems.
    page.get_by_role("button", name="Client setup").click()
    page.get_by_role("button", name="Show this client's settings").click()
    playwright_api.expect(page.locator("#settings")).to_have_value(re.compile("sahra-express"))
    settings = json.loads(page.locator("#settings").input_value())
    settings["money"]["late_penalty_per_parcel"] = -5
    settings["hubs"][0]["lat"] = 125
    page.locator("#settings").fill(json.dumps(settings))
    page.get_by_role("button", name="Save changes to this client").click()
    see(page, "Late penalty must not be negative")
    see(page, "Hub location isn't a valid coordinate")
    shot(page, "6-setup")

    assert page.errors == []


def test_text_from_a_file_is_shown_as_text_and_never_run(page, server):
    # A file can contain anything, so the screen must never treat it as code.
    row = (SAMPLE_DATA / "consignments.csv").read_text(encoding="utf-8-sig").splitlines()
    row[1] = row[1].replace("C0371", "<img src=x onerror=window.hacked=1>")
    files = {
        "consignments": ("c.csv", "\n".join(row).encode(), "text/csv"),
        "drivers": ("d.csv", (SAMPLE_DATA / "drivers.csv").read_bytes(), "text/csv"),
        "vehicles": ("v.csv", (SAMPLE_DATA / "vehicles.csv").read_bytes(), "text/csv"),
    }
    assert httpx.post(f"{server}/clients/sahra-express/hubs/AQ3/shifts/2026-10-15/uploads", files=files).status_code == 201
    page.locator("#date").fill("2026-10-15")
    page.locator("#date").dispatch_event("change")
    page.get_by_role("button", name="1. Check today's files").click()
    see(page, "What we found")
    page.get_by_text("Other messages").click()
    page.wait_for_timeout(300)

    assert page.evaluate("window.hacked") is None
