import functools
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
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
TEST_REPLY_MESSAGE = "Your bottle found me at just the right time. Thank you for sending it into the world."


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


def _all_stored_bottles(exclude_account_id=None):
    bottles = []

    if storage is None:
        return bottles

    if storage.database_enabled():
        try:
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    if exclude_account_id:
                        cur.execute(
                            """
                            SELECT account_id, month_key, state
                            FROM bottles
                            WHERE account_id <> %s
                            ORDER BY updated_at DESC
                            """,
                            (exclude_account_id,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT account_id, month_key, state
                            FROM bottles
                            ORDER BY updated_at DESC
                            """
                        )
                    rows = cur.fetchall()

            for account_id, month_key, state in rows:
                bottle = dict(state)
                storage.attach_account(bottle, account_id, month_key)
                bottles.append((account_id, bottle))
        except Exception as exc:
            print(f"Beach finder database scan failed: {exc}")

        return bottles

    if not os.path.exists(storage.SAVE_FILE):
        return bottles

    try:
        with open(storage.SAVE_FILE, "r") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"Beach finder local scan failed: {exc}")
        return bottles

    if isinstance(data, dict) and isinstance(data.get("bottles"), dict):
        for key, bottle in data["bottles"].items():
            try:
                account_id, month_key = key.split("|", 1)
            except ValueError:
                continue

            if exclude_account_id and account_id == exclude_account_id:
                continue

            bottle = dict(bottle)
            storage.attach_account(bottle, account_id, month_key)
            bottles.append((account_id, bottle))

    return bottles


def _eligible_test_beach_bottle(exclude_account_id=None):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for account_id, bottle in _all_stored_bottles(exclude_account_id):
        if bottle.get("status") != "ashore":
            continue
        if bottle.get("opened"):
            continue
        if not bottle.get("finder_eligible", True):
            continue

        try:
            if not engine.bottle_is_mature(bottle, check_time=now):
                continue
        except Exception:
            pass

        return account_id, bottle

    return None, None


def _create_test_beach_bottle():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    launch_time = (now - timedelta(days=3)).isoformat()
    account_id = f"beach-test-{uuid.uuid4()}"

    bottle = engine.create_bottle(
        29.0,
        -88.0,
        start_time=launch_time,
        sender_id=f"beach-sender-{uuid.uuid4()}",
        message="I wonder who will find this bottle.",
    )

    storage.attach_account(
        bottle,
        account_id,
        now.strftime("%Y-%m"),
    )

    bottle["status"] = "ashore"
    bottle["finder_eligible"] = True
    bottle["opened"] = False
    bottle["ashore_time"] = (now - timedelta(hours=2)).isoformat()
    bottle["eligible_after"] = (now - timedelta(hours=1)).isoformat()
    bottle["current_time"] = now.isoformat()
    bottle["total_miles_traveled"] = 42.0
    bottle["journey_areas"] = ["Gulf of Mexico"]

    try:
        engine.add_journey_event(
            bottle,
            "washed_ashore",
            event_time=bottle["ashore_time"],
        )
    except Exception as exc:
        print(f"Could not add test washed-ashore event: {exc}")

    storage.save_bottle(bottle)
    return account_id, bottle


def _install_beach_test_route(app):
    try:
        from fastapi import Body, HTTPException
    except Exception:
        return

    for route in getattr(app, "routes", []):
        if getattr(route, "path", None) == "/api/test/beach-walk/find":
            return

    @app.post("/api/test/beach-walk/find")
    def test_beach_walk_find(payload: dict = Body(...)):
        account_id = payload.get("account_id")
        finder_id = payload.get("finder_id")

        if not isinstance(account_id, str) or not account_id.startswith("ios-test-"):
            raise HTTPException(status_code=403, detail="Test beach finder requires an iOS test account.")

        if not finder_id:
            raise HTTPException(status_code=400, detail="finder_id is required.")

        found_account_id, bottle = _eligible_test_beach_bottle(
            exclude_account_id=account_id
        )

        if bottle is None:
            found_account_id, bottle = _create_test_beach_bottle()

        return {
            "bottle_found": True,
            "found_account_id": found_account_id,
            "bottle_id": bottle.get("bottle_id"),
            "status": bottle.get("status", "ashore"),
            "total_miles_traveled": round(bottle.get("total_miles_traveled", 0.0), 2),
            "journey_areas": list(bottle.get("journey_areas", [])),
        }


def _wrap_bottle_get(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        result = func(*args, **kwargs)

        if not isinstance(result, dict) or storage is None:
            return result

        account_id = kwargs.get("account_id")
        if account_id is None and args:
            account_id = args[0]
        account_id = account_id or "demo-account"

        try:
            bottle = storage.load_bottle(account_id)
        except Exception as exc:
            print(f"Reply return lookup skipped: {exc}")
            return result

        if bottle is not None:
            result["reply_message"] = bottle.get("reply_message")
            result["reply_time"] = bottle.get("reply_time")

        return result

    return wrapped


if engine is not None:
    engine.move_bottle_live = _move_bottle_live_remote
    _original_shipping_density = engine.shipping_density_at
    engine.shipping_density_at = _safe_shipping_density


if storage is not None:
    _original_load_bottle = storage.load_bottle

    def load_bottle_with_catchup(account_id=None, month_key=None):
        bottle = _original_load_bottle(account_id, month_key)
        bottle = _catch_up_bottle(bottle, account_id=account_id)

        if (
            bottle is not None
            and isinstance(account_id, str)
            and account_id.startswith("ios-test-")
            and not bottle.get("reply_message")
        ):
            bottle["reply_message"] = TEST_REPLY_MESSAGE
            bottle["reply_time"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            try:
                storage.save_bottle(bottle)
            except Exception as exc:
                print(f"Test reply save skipped: {exc}")

        return bottle

    storage.load_bottle = load_bottle_with_catchup


try:
    from fastapi import FastAPI

    _original_fastapi_init = FastAPI.__init__
    _original_fastapi_get = FastAPI.get

    def _fastapi_init_with_beach_test(self, *args, **kwargs):
        _original_fastapi_init(self, *args, **kwargs)
        _install_beach_test_route(self)

    def _fastapi_get_with_reply(self, path, *args, **kwargs):
        decorator = _original_fastapi_get(self, path, *args, **kwargs)

        def install(func):
            if path == "/api/bottle":
                func = _wrap_bottle_get(func)
            return decorator(func)

        return install

    FastAPI.__init__ = _fastapi_init_with_beach_test
    FastAPI.get = _fastapi_get_with_reply
except Exception as exc:
    print(f"Startup patch install skipped: {exc}")
