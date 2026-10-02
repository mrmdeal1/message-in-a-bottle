import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

try:
    import bottle_engine as engine
    import storage
except Exception:
    engine = None
    storage = None


CURRENT_WORKER_URL = "https://message-in-a-bottle-currents.onrender.com/current"
FORCE_TEST_ACCOUNT = "ios-test-E07DE60C-A24E-4D48-A757-E0E11CDBBBDD"


def _move_bottle_live_remote(lat, lon, date, hours=6):
    query = urlencode({
        "lat": lat,
        "lon": lon,
        "date": date,
    })

    with urlopen(f"{CURRENT_WORKER_URL}?{query}", timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))

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


def _as_utc(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _catch_up_bottle(bottle, account_id=None):
    if bottle is None:
        return None

    if bottle.get("opened"):
        return bottle

    if bottle.get("status") != "drifting":
        return bottle

    current_value = bottle.get("current_time")
    if not current_value:
        return bottle

    force_once = (
        account_id == FORCE_TEST_ACCOUNT
        and not bottle.get("_forced_six_hour_test_done", False)
    )

    if force_once:
        try:
            engine.advance_bottle(
                bottle,
                total_hours=6,
                step_hours=6,
            )
            bottle["_forced_six_hour_test_done"] = True
            storage.save_bottle(bottle)
        except Exception as exc:
            print(f"Forced six-hour test skipped: {exc}")
        return bottle

    try:
        journey_time = _as_utc(current_value)
    except (TypeError, ValueError):
        return bottle

    now = datetime.now(timezone.utc)
    elapsed_hours = int((now - journey_time).total_seconds() // 3600)

    # Use the engine's normal six-hour movement cadence.
    catchup_hours = (elapsed_hours // 6) * 6

    if catchup_hours <= 0:
        return bottle

    # Keep any single read bounded. Additional reads continue catching up.
    catchup_hours = min(catchup_hours, 24)

    try:
        engine.advance_bottle(
            bottle,
            total_hours=catchup_hours,
            step_hours=6,
        )
        storage.save_bottle(bottle)
    except Exception as exc:
        # Temporary ocean-data trouble should not make the bottle unreadable.
        print(f"Bottle catch-up skipped: {exc}")

    return bottle


if engine is not None:
    engine.move_bottle_live = _move_bottle_live_remote


if storage is not None:
    _original_load_bottle = storage.load_bottle

    def load_bottle_with_catchup(account_id=None, month_key=None):
        bottle = _original_load_bottle(account_id, month_key)
        return _catch_up_bottle(bottle, account_id=account_id)

    storage.load_bottle = load_bottle_with_catchup
