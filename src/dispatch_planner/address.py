"""Works out where each consignment is going, and how sure we are."""

import math
import re
import statistics
from collections import defaultdict

from dispatch_planner.geocoding import GeocodingError, NoMapService
from dispatch_planner.upload import UploadRejected, read_rows

_MAX_AREA_TYPOS = 2
_MIN_FUZZY_LENGTH = 5
_MANY_DELIVERIES = 3

_CALL_ON_ARRIVAL = "Driver calls the customer on arrival"

ASK_FOR_ADDRESS = "Please send us your full delivery address, including the building and flat or villa number."
ASK_TO_CONFIRM = "Please confirm your delivery address, including the area and building name."


_APPROXIMATE_SOURCES = {"area_centre", "disagreeing_history", "phone_only", "same_phone_today", "no_location"}
_MISSING_FULL_ADDRESS = "full address"
_MISSING_BUILDING = "building name"
_MISSING_UNIT = "flat or villa number"
_MISSING_STREET = "street number"


def distance_m(lat_a, lng_a, lat_b, lng_b):
    radians = math.radians
    a = (
        math.sin(radians(lat_b - lat_a) / 2) ** 2
        + math.cos(radians(lat_a)) * math.cos(radians(lat_b)) * math.sin(radians(lng_b - lng_a) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(math.sqrt(a))


def _normalise(text):
    return " ".join(re.sub(r"[^\w ]", " ", text.casefold()).split())


def _typos(a, b):
    """Edit distance where swapping two neighbouring letters counts as one mistake."""
    grid = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        grid[i][0] = i
    for j in range(len(b) + 1):
        grid[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            grid[i][j] = min(grid[i - 1][j] + 1, grid[i][j - 1] + 1, grid[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                grid[i][j] = min(grid[i][j], grid[i - 2][j - 2] + 1)
    return grid[-1][-1]


def _rows_or_reject(settings, name, data):
    problems = []
    rows = read_rows(settings, name, data, problems)
    if problems:
        raise UploadRejected(problems)
    return [row for _, row in rows]


def _coordinate(text):
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def load_areas(settings, data):
    """Read a client's area list from CSV."""
    separator = settings["address"]["alias_separator"]
    areas = []
    for row in _rows_or_reject(settings, "areas", data):
        lat, lng = _coordinate(row["centroid_lat"]), _coordinate(row["centroid_lng"])
        if row["area_name"] and lat is not None and lng is not None:
            aliases = [alias.strip() for alias in row.get("aliases", "").split(separator) if alias.strip()]
            areas.append(
                {"area_name": row["area_name"], "aliases": aliases, "region": row["emirate"], "lat": lat, "lng": lng}
            )
    if not areas:
        raise UploadRejected([{"path": "areas", "message": "File has no usable areas"}])
    return areas


class AreaLookup:
    def __init__(self, settings, areas):
        self._by_name = defaultdict(dict)
        for area in areas:
            for name in [area["area_name"], *area["aliases"]]:
                self._by_name[_normalise(name)][area["area_name"]] = area
        self._cache = {}

    def resolve(self, text):
        """Return (area, how), where how is empty, exact, fuzzy, multiple, or none."""
        key = _normalise(text)
        if key not in self._cache:
            self._cache[key] = self._resolve(key)
        return self._cache[key]

    def candidates(self, text):
        """Every area an exact name could mean."""
        return list(self._by_name.get(_normalise(text), {}).values())

    def _resolve(self, key):
        if not key:
            return None, "empty"
        if key in self._by_name:
            matches = self._by_name[key]
            return (next(iter(matches.values())), "exact") if len(matches) == 1 else (None, "multiple")
        if len(key) < _MIN_FUZZY_LENGTH:
            return None, "none"
        scored = [(_typos(key, name), name) for name in self._by_name if abs(len(name) - len(key)) <= _MAX_AREA_TYPOS]
        if not scored:
            return None, "none"
        best = min(score for score, _ in scored)
        if best > _MAX_AREA_TYPOS:
            return None, "none"
        matches = {}
        for score, name in scored:
            if score == best:
                matches.update(self._by_name[name])
        return (next(iter(matches.values())), "fuzzy") if len(matches) == 1 else (None, "none")


def _place_name(settings, text):
    replacements = {_normalise(word): target for word, target in settings["address"]["word_replacements"].items()}
    return " ".join(replacements.get(word, word) for word in _normalise(text).split())


def _is_villa(settings, unit):
    first = unit.split()[0] if unit.split() else ""
    return any(first == word or re.fullmatch(rf"{re.escape(word)}\d+", first) for word in settings["address"]["villa_words"])


def _read_address(settings, lookup, fields):
    """Work out the unit, building, and area from a row's address fields."""
    unit = fields.get("address_line_1", "")
    building = fields.get("address_line_2", "")
    area_text = fields.get("area", "")
    notes = fields.get("notes", "")

    for marker in settings["address"]["note_address_markers"]:
        if marker in notes:
            parts = [part.strip() for part in notes.split(marker)[0].split(",")]
            area_text = parts[-1]
            if _is_villa(settings, _place_name(settings, parts[0])):
                unit, building = ", ".join(parts[:2]), ""
            else:
                unit, building = parts[0], parts[-2] if len(parts) > 2 else ""
            break
    else:
        if "," in unit and not building and not area_text:
            unit, building, area_text = _split_packed_line(settings, lookup, unit)

    area, how = lookup.resolve(area_text)
    if area is None:
        city_area, city_how = lookup.resolve(fields.get("city", ""))
        if city_area is not None or how == "empty":
            area, how = city_area, city_how
    unit_name = _place_name(settings, unit)
    building_name = _place_name(settings, building)
    if set(building_name.split()) & set(settings["address"]["completeness"]["not_building_words"]):
        building_name = ""
    return {
        "raw_unit": unit.strip(),
        "unit": unit_name,
        "building": building_name,
        "area": area,
        "how": how,
        "candidates": lookup.candidates(area_text) if how == "multiple" else [],
    }


def _split_packed_line(settings, lookup, line):
    parts = [part.strip() for part in line.split(",")]
    for index, part in enumerate(parts):
        if index > 0 and lookup.resolve(part)[0] is not None:
            if _is_villa(settings, _place_name(settings, parts[0])):
                return ", ".join(parts[:2]), parts[2] if index > 2 else "", part
            return parts[0], parts[1] if index > 1 else "", part
    return line, "", ""


def _missing_detail(settings, address):
    """What the text lacks to look like an address, or None. This checks format, not that the place exists."""
    config = settings["address"]
    words = config["completeness"]
    raw, unit, building = address["raw_unit"], address["unit"], address["building"]
    if raw.casefold() in {text.casefold() for text in config["unusable_text"]}:
        return _MISSING_UNIT if building else _MISSING_FULL_ADDRESS
    if not unit:
        return _MISSING_UNIT if building else _MISSING_FULL_ADDRESS
    if any(f" {_normalise(phrase)} " in f" {unit} {building} " for phrase in config["vague_phrases"]):
        return _MISSING_FULL_ADDRESS
    if _is_villa(settings, unit):
        return None if len(re.findall(r"\d+", unit)) >= 2 else _MISSING_STREET

    code_word = config.get("location_code_word")
    if code_word and unit.split()[0] == _normalise(code_word):
        complete = len(re.sub(r"\D", "", unit)) == 10
        return (None if building else _MISSING_BUILDING) if complete else _MISSING_FULL_ADDRESS

    tokens = re.sub(r"^([^\W\d]+)(\d)", r"\1 \2", unit).split()
    label, rest = tokens[0], [token for token in tokens[1:] if token not in words["number_prefixes"]]
    if raw[0] in words["unit_words"] and label.isdigit():
        label, rest = raw[0], tokens
    if not rest or not rest[0].isdigit() or len(rest[0]) > words["max_unit_digits"]:
        return _MISSING_FULL_ADDRESS
    if label in words["street_unit_words"]:
        return None if len(re.findall(r"\d+", unit)) >= 2 else _MISSING_STREET
    if label not in words["unit_words"]:
        return _MISSING_FULL_ADDRESS
    return None if building else _MISSING_BUILDING


def _is_vague(settings, address):
    return _missing_detail(settings, address) is not None


def _place_keys(settings, address):
    area_name = address["area"]["area_name"]
    if _is_villa(settings, address["unit"]):
        numbers = re.findall(r"\d+", address["unit"])
        return [("villa", numbers[0], numbers[1], area_name)] if len(numbers) >= 2 else []
    return [("building", address["building"], area_name)] if address["building"] else []


def clean_history(settings, areas, data):
    """Read delivery history, fix what can be fixed, and drop what can't be trusted."""
    config = settings["address"]
    lookup = AreaLookup(settings, areas)
    hubs = [(hub["lat"], hub["lng"]) for hub in settings["hubs"]]
    unreliable = set(config["unreliable_failures"])
    rows = _rows_or_reject(settings, "delivery_history", data)
    report = {"received": len(rows), "swapped": 0, "unreadable_removed": 0, "at_hub_removed": 0, "unreliable_removed": 0}

    def nearest_hub(lat, lng):
        return min(distance_m(lat, lng, *hub) for hub in hubs)

    events = []
    for row in rows:
        lat, lng = _coordinate(row["event_lat"]), _coordinate(row["event_lng"])
        if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
            report["unreadable_removed"] += 1
            continue
        if abs(lng) <= 90 and nearest_hub(lng, lat) < nearest_hub(lat, lng):
            lat, lng = lng, lat
            report["swapped"] += 1
        if row["failure_reason"] in unreliable:
            report["unreliable_removed"] += 1
            events.append({**row, "lat": lat, "lng": lng, "location_unreliable": True})
            continue
        if nearest_hub(lat, lng) < config["hub_exclusion_m"]:
            area = _read_address(settings, lookup, row)["area"]
            if area is None or nearest_hub(area["lat"], area["lng"]) > config["hub_area_radius_m"]:
                report["at_hub_removed"] += 1
                continue
        events.append({**row, "lat": lat, "lng": lng})
    report["kept"] = sum(not event.get("location_unreliable") for event in events)
    return events, report


class _History:
    def __init__(self, settings, lookup, events):
        self.by_phone = defaultdict(list)
        self.by_phone_area = defaultdict(list)
        self.by_place = defaultdict(list)
        self.address_failures = defaultdict(int)
        for event in events:
            address = _read_address(settings, lookup, event)
            phone = event["customer_phone"]
            if event.get("location_unreliable"):
                if phone and address["area"]:
                    self.address_failures[(phone, address["area"]["area_name"])] += 1
                continue
            if phone:
                self.by_phone[phone].append(event)
            if address["area"]:
                if phone:
                    self.by_phone_area[(phone, address["area"]["area_name"])].append(event)
                for key in _place_keys(settings, address):
                    self.by_place[key].append(event)


def _deliveries(events):
    return "1 past delivery" if len(events) == 1 else f"{len(events)} past deliveries"


def _middle(events):
    return statistics.median(e["lat"] for e in events), statistics.median(e["lng"] for e in events)


def _spread(events, middle):
    return statistics.median(distance_m(e["lat"], e["lng"], *middle) for e in events)


def _past_address(settings, lookup, events):
    for event in reversed(events):
        address = _read_address(settings, lookup, event)
        if not _is_vague(settings, address):
            parts = [event.get("address_line_1", ""), event.get("address_line_2", ""), event.get("area", "")]
            return ", ".join(part for part in parts if part)
    return None


def assess_shift(settings, consignments, areas, events, map_service=None, plan_date=None):
    """Assess every consignment except rejected rows."""
    map_service = map_service or NoMapService()
    lookup = AreaLookup(settings, areas)
    history = _History(settings, lookup, events)
    rows = [row for row in consignments if row["result"] != "rejected"]

    assessments = [_assess_row(settings, lookup, history, map_service, row) for row in rows]

    good_by_phone = {}
    for row, assessment in zip(rows, assessments):
        phone = row["values"]["customer_phone"]
        if phone and assessment["quality"] == "GOOD":
            good_by_phone.setdefault(phone, assessment)
    for row, assessment in zip(rows, assessments):
        same_phone = good_by_phone.get(row["values"]["customer_phone"])
        if same_phone and assessment["lat"] is None:
            _set_location(
                settings, assessment, "AMBIGUOUS", "same_phone_today", same_phone["lat"], same_phone["lng"],
                settings["address"]["confidence"]["phone_only"],
                f"Same phone as {same_phone['consignment_id']} today",
            )
    for row, assessment in zip(rows, assessments):
        _set_verification(settings, row, assessment, plan_date)
    return assessments


def reassess_with_area(settings, row, areas, events, area_name, map_service=None, plan_date=None):
    """Assess one consignment again, using the area its customer gave."""
    lookup = AreaLookup(settings, areas)
    values = {**row["values"], "area": area_name, "city": ""}
    assessment = _assess_row(settings, lookup, _History(settings, lookup, events), map_service or NoMapService(), {**row, "values": values})
    _set_verification(settings, {**row, "values": values}, assessment, plan_date)
    if assessment["verification"]:
        assessment["verification"]["status"] = "answered"
    else:
        assessment["verification"] = {"status": "answered", "message": None, "question": None}
    return assessment


def _set_verification(settings, row, assessment, plan_date):
    """The confirmation a customer would get. Nothing is sent from this prototype."""
    if assessment["source"] not in _APPROXIMATE_SOURCES:
        assessment["verification"] = None
        return
    config = settings["address"]["verification"]
    question = _question(config, assessment)
    message = config["message"].format(
        client=settings["name"], consignment_id=row["id"], date=plan_date or "your delivery date", question=question
    )
    status = "whatsapp_triggered" if row["values"]["customer_phone"] else "needs_agent"
    assessment["verification"] = {"status": status, "question": question, "message": message}


def _question(config, assessment):
    if assessment.get("area_options"):
        return config["which_area_question"].format(areas=" or ".join(assessment["area_options"]))
    if not assessment["area"] and assessment.get("building"):
        return config["area_question"].format(building=assessment["building"].title())
    if assessment.get("missing"):
        return config["missing_question"].format(missing=assessment["missing"])
    return config["door_question"]


def _assess_row(settings, lookup, history, map_service, row):
    values = row["values"]
    address = _read_address(settings, lookup, values)
    resolved_by_history = _area_from_history(address, history)
    if resolved_by_history:
        address.update(area=resolved_by_history, how="history", candidates=[])
    area = address["area"]
    missing = _missing_detail(settings, address)
    assessment = {
        "consignment_id": row["id"],
        "area": area["area_name"] if area else None,
        "unit": address["unit"],
        "building": address["building"],
        "area_options": [candidate["area_name"] for candidate in address["candidates"]],
        "missing": missing,
        "area_centre": {"lat": area["lat"], "lng": area["lng"]} if area else None,
        "location_code": _location_code(settings, values),
        "past_address": None,
        "findings": _findings(settings, values, area),
        "vague": missing is not None,
        "geocoding": "not_needed",
    }
    if area:
        _locate_with_area(settings, lookup, history, map_service, address, values, assessment)
    else:
        _locate_without_area(settings, history, address, values, assessment)
    _warn_about_repeated_failures(settings, history, values, assessment)
    return assessment


def _area_from_history(address, history):
    """An unclear area name is settled when past deliveries place the building in only one of its areas."""
    if not address["building"]:
        return None
    known = [area for area in address["candidates"] if ("building", address["building"], area["area_name"]) in history.by_place]
    return known[0] if len(known) == 1 else None


def _warn_about_repeated_failures(settings, history, values, assessment):
    failures = history.address_failures.get((values["customer_phone"], assessment["area"]), 0)
    if values["customer_phone"] and failures >= settings["address"]["verification"]["repeat_failure_threshold"]:
        message = f"{failures} past \"address not found\" failures for this customer in {assessment['area']}"
        assessment["findings"].append(_finding("repeated_address_failures", message))
        if assessment["action_owner"] is None:
            assessment.update(action=_CALL_ON_ARRIVAL, action_owner="driver")


def _geocode(settings, map_service, address, assessment):
    """Ask the map service, and accept its answer only if it passes all four checks."""
    config = settings["address"]["verification"]
    area = address["area"]
    if assessment["missing"]:
        assessment["geocoding"] = "skipped_incomplete_text"
        return None
    is_villa = _is_villa(settings, address["unit"])
    query = {
        "place": address["raw_unit"] if is_villa else address["building"],
        "area": area["area_name"],
        "region": area["region"],
        "country_code": config["country_code"],
    }
    try:
        result = map_service.locate(query)
    except GeocodingError:
        assessment["geocoding"] = "service_error"
        return None
    if result is None:
        assessment["geocoding"] = "not_found"
        return None
    failure = _geocode_failure(config, area, result)
    assessment["geocoding"] = failure or "accepted"
    return None if failure else result


def _geocode_failure(config, area, result):
    from_centre = distance_m(result["lat"], result["lng"], area["lat"], area["lng"])
    if not result["specific"]:
        return "rejected_not_specific"
    if not result["full_match"]:
        return "rejected_partial_match"
    if from_centre > config["max_area_distance_m"]:
        return "rejected_too_far"
    if from_centre < config["min_centre_distance_m"]:
        return "rejected_area_centre"
    return None


def _locate_with_area(settings, lookup, history, map_service, address, values, assessment):
    confidence = settings["address"]["confidence"]
    limit = settings["address"]["max_spread_m"]
    area = address["area"]
    phone = values["customer_phone"]

    groups = []
    if phone and (phone, area["area_name"]) in history.by_phone_area:
        groups.append(("phone_and_area", history.by_phone_area[(phone, area["area_name"])]))
    for key in _place_keys(settings, address):
        if key in history.by_place:
            groups.append(("same_place", history.by_place[key]))

    for source, events in groups:
        middle = _middle(events)
        if _spread(events, middle) <= limit:
            if source == "phone_and_area":
                level = "phone_area_many" if len(events) >= _MANY_DELIVERIES else "phone_area_few"
                reason = f"{_deliveries(events)} to this customer in {area['area_name']}"
            else:
                level = "same_place"
                reason = f"{_deliveries(events)} to this building or villa"
            assessment["past_address"] = _past_address(settings, lookup, events)
            _set_location(settings, assessment, "GOOD", source, *middle, confidence[level], reason)
            return

    located = _geocode(settings, map_service, address, assessment)
    if located:
        reason = f"Geocoder found this address in {area['area_name']} and it passed all checks"
        _set_location(settings, assessment, "GOOD", "map_service", located["lat"], located["lng"], confidence["geocoded"], reason)
        return

    if groups:
        middle = _middle(groups[0][1])
        reason = "Past deliveries are too far apart to trust"
        _set_location(settings, assessment, "AMBIGUOUS", "disagreeing_history", *middle, confidence["disagreeing"], reason)
        return

    reason = f"No delivery evidence, so we used the centre of {area['area_name']}"
    _set_location(settings, assessment, "INCOMPLETE", "area_centre", area["lat"], area["lng"], confidence["area_centre"], reason)


def _locate_without_area(settings, history, address, values, assessment):
    confidence = settings["address"]["confidence"]
    phone = values["customer_phone"]
    events = history.by_phone.get(phone, []) if phone else []
    if events:
        middle = _middle(events)
        if _spread(events, middle) <= settings["address"]["max_spread_m"]:
            reason = f"{_deliveries(events)} to this phone, but no area in this address"
            _set_location(settings, assessment, "AMBIGUOUS", "phone_only", *middle, confidence["phone_only"], reason)
            return
    if address["how"] == "multiple":
        _set_location(settings, assessment, "AMBIGUOUS", "no_location", None, None, 0, "The area name matches more than one area")
    elif address["building"]:
        _set_location(settings, assessment, "AMBIGUOUS", "no_location", None, None, 0, "A building is named, but no area")
    else:
        _set_location(settings, assessment, "INVALID", "no_location", None, None, 0, "Nothing usable in this address")


def _set_location(settings, assessment, quality, source, lat, lng, confidence, reason):
    assessment.update(quality=quality, source=source, lat=lat, lng=lng, confidence=confidence, reason=reason)
    assessment["routable"] = lat is not None

    vague = assessment["vague"]
    if source == "map_service":
        owner, action, message = "driver", _CALL_ON_ARRIVAL, None
    elif quality == "GOOD":
        owner = "driver" if vague and assessment["past_address"] else None
        action = "Show the past address to the driver" if owner else "None"
        message = None
    elif quality == "INCOMPLETE" and not vague:
        owner, action, message = "driver", _CALL_ON_ARRIVAL, None
    else:
        owner = "customer_service"
        action = "Contact the customer before dispatch"
        if lat is None:
            action += "; unassigned until confirmed"
        else:
            action += "; if unresolved, the driver calls first"
        message = ASK_FOR_ADDRESS if quality == "INVALID" or vague else ASK_TO_CONFIRM
    assessment.update(action=action, action_owner=owner, message=message)


def _location_code(settings, values):
    word = settings["address"].get("location_code_word")
    if not word:
        return None
    text = " ".join(values.get(key, "") for key in ("address_line_1", "address_line_2", "landmark", "notes"))
    match = re.search(rf"{re.escape(word)}\D*((?:\d[ -]?){{10}})", text, re.IGNORECASE)
    return re.sub(r"\D", "", match[1]) if match else None


def _findings(settings, values, area):
    config = settings["address"]
    findings = []
    if area and area["region"] not in config["service_area"]:
        findings.append(_finding("outside_service_area", f"{area['area_name']} is outside the service area"))

    notes = values.get("notes", "").casefold()
    start, end = values.get("slot_start"), values.get("slot_end")
    for rule in config["note_conflicts"]:
        if rule["phrase"].casefold() in notes and start and end:
            too_late = "start_before" in rule and start >= rule["start_before"]
            too_early = "end_after" in rule and end <= rule["end_after"]
            if too_late or too_early:
                message = f"Note says \"{rule['phrase']}\", but the booked slot is {start}–{end}"
                findings.append(_finding("note_conflicts_with_slot", message))

    if values.get("payment_type") == "COD":
        for phrase in config["cod_note_conflicts"]:
            if phrase.casefold() in notes:
                findings.append(_finding("note_conflicts_with_cod", f"Note says \"{phrase}\", but this order needs payment"))
    return findings


def _finding(code, message):
    return {"code": code, "message": message}


def confirm_location(settings, assessment, lat, lng):
    """Record a location confirmed by customer service."""
    confidence = settings["address"]["confidence"]["confirmed"]
    assessment["vague"] = False
    _set_location(settings, assessment, "GOOD", "confirmed", lat, lng, confidence, "Confirmed by customer service")
    if assessment.get("verification"):
        assessment["verification"]["status"] = "answered"
    return assessment


def score_accuracy(settings, assessments, data):
    """Compare assessments with verified locations. Never changes an assessment."""
    verified = {}
    for row in _rows_or_reject(settings, "verified_locations", data):
        lat, lng = _coordinate(row["true_lat"]), _coordinate(row["true_lng"])
        if lat is not None and lng is not None:
            verified[row["consignment_id"]] = (lat, lng)
    matched = [a for a in assessments if a["consignment_id"] in verified]

    def errors(group, point):
        found = []
        for assessment in group:
            location = point(assessment)
            if location[0] is not None:
                found.append(distance_m(*verified[assessment["consignment_id"]], *location))
        return found

    def own_location(assessment):
        return assessment["lat"], assessment["lng"]

    def area_centre(assessment):
        centre = assessment["area_centre"]
        return (centre["lat"], centre["lng"]) if centre else (None, None)

    by_quality = {}
    for quality in sorted({a["quality"] for a in matched}):
        group = [a for a in matched if a["quality"] == quality]
        by_quality[quality] = _summary(len(group), errors(group, own_location))
    return {
        "overall": _summary(len(matched), errors(matched, own_location)),
        "area_centre_baseline": _summary(len(matched), errors(matched, area_centre)),
        "by_quality": by_quality,
    }


def _summary(verified, errors):
    return {
        "verified": verified,
        "located": len(errors),
        "median_m": round(statistics.median(errors)) if errors else None,
        "within_100m": sum(error <= 100 for error in errors),
        "within_250m": sum(error <= 250 for error in errors),
        "worst_m": round(max(errors)) if errors else None,
    }
