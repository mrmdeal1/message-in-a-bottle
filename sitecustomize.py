import json
import math
from urllib.parse import urlencode
from urllib.request import urlopen

try:
    import bottle_engine as engine
except Exception:
    engine = None


CURRENT_WORKER_URL = "https://message-in-a-bottle-currents.onrender.com/current"


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


if engine is not None:
    engine.move_bottle_live = _move_bottle_live_remote
