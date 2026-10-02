import json
import math
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

try:
    import bottle_engine as engine
    import storage
except Exception:
    engine = None
    storage = None


CURRENT_WORKER_URL = "https://message-in-a-bottle-currents.onrender.com/current"
CURRENT_WORKER_HEALTH_URL = "https://message-in-a-bottle-currents.onrender.com/health"


def _wake_current_worker():
    try:
        with urlopen(CURRENT_WORKER_HEALTH_URL, timeout=20) as response:
            response.read()
    except Exception as exc:
        print(f"Current worker wake check failed: {exc}")


def _fetch_current_payload(lat, lon, date):
    query = urlencode({
        "lat": lat,
        "lon": lon,
        "date": date,
    })
    url = f"{CURRENT_WORKER_URL}?{query}"

    last_exc = None
    delays = (0, 2, 5)

    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)

        try:
            with urlopen(url, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            last_exc = exc
            print(f"Current worker attempt {attempt} failed: {exc}")
            if attempt == 1:
                _wake_current_worker()

    raise last_exc


def _move_bottle_live_remote(lat, lon, date, hours=6):
    payload = _fetch_current_payload(lat, lon, date)

    u = float(payload["u"])
    v = float(payload["v"])

    seconds = hours * 3600
    east_m = u * seconds
    north_m = v * seconds

    new_lat = lat + north_m / 111320
    new_lon = lon + east_m / (
        111320 * math.cos(math.radians(lat))
    )

    miles = math.hypot(east_m, north_m) / 1609.344
    return new_lat, new_lon, miles


def _safe_shipping_density(lat, lon):
    try:
        return _original_shipping_density(lat, lon)
    except Exception as exc:
        print(f"Shipping density unavailable; using none: {exc}")
        return 0


def _as_utc(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prepare_shoreline_ui_test(bottle, account_id=None):
    if bottle is None:
        return None

    areas = bottle.get("journey_areas", [])
    miles = float(bottle.get("total_miles_traveled", 0.0) or 0.0)

    is_current_ios_test = (
        isinstance(account_id, str)
        and account_id.startswith("ios-test-")
        and bottle.get("status") == "drifting"
        and not bottle.get("opened", False)
        and 9.0 <= miles <= 11.5
        and bool(areas)
        and areas[-1] == "Gulf of Mexico"
        and not bottle.get("_shoreline_ui_test_done", False)
    )

    if not is_current_ios_test:
        return bottle

    bottle["_shoreline_ui_test_done"] = True
    bottle["status"] = "ashore"
    bottle["finder_eligible"] = True
    bottle["ashore_time"] = bottle.get("current_time")
    bottle["eligible_after"] = bottle.get("current_time")

    try:
        engine.add_journey_event(
            bottle,
            "washed_ashore",
            event_time=bottle.get("current_time"),
        )
    except Exception as exc:
        print(f"Could not add shoreline test event: {exc}")

    storage.save_bottle(bottle)
    print("Prepared one-time shoreline UI test.")
    return bottle


def _catch_up_bottle(bottle, account_id=None):
    if bottle is None:
        return None

    bottle = _prepare_shoreline_ui_test(
        bottle,
        account_id=account_id,
    )

    if bottle.get("opened"):
        return bottle

    if bottle.get("status") != "drifting":
        return bottle

    current_value = bottle.get("current_time")
    if not current_value:
        return bottle

    try:
        journey_time = _as_utc(current_value)
    except (TypeError, ValueError):
        return bottle

    now = datetime.now(timezone.utc)
    elapsed_hours = int((now - journey_time).total_seconds() // 3600)

    catchup_hours = (elapsed_hours // 6) * 6

    if catchup_hours <= 0:
        return bottle

    catchup_hours = min(catchup_hours, 24)

    try:
        engine.advance_bottle(
            bottle,
            total_hours=catchup_hours,
            step_hours=6,
        )
        storage.save_bottle(bottle)
    except Exception as exc:
        print(f"Bottle catch-up skipped: {exc}")

    return bottle


if engine is not None:
    engine.move_bottle_live = _move_bottle_live_remote
    _original_shipping_density = engine.shipping_density_at
    engine.shipping_density_at = _safe_shipping_density


if storage is not None:
    _original_load_bottle = storage.load_bottle

    def load_bottle_with_catchup(account_id=None, month_key=None):
        bottle = _original_load_bottle(account_id, month_key)
        return _catch_up_bottle(bottle, account_id=account_id)

    storage.load_bottle = load_bottle_with_catchup
