import math

try:
    import bottle_engine as engine
except Exception:
    engine = None


def _move_bottle_live_safe(lat, lon, date, hours=6):
    local_currents = engine.get_currents(lat, lon, date)

    try:
        point = local_currents.sel(
            latitude=lat,
            longitude=lon,
            method="nearest",
        )
        u = float(point["uo"].values.squeeze())
        v = float(point["vo"].values.squeeze())
    finally:
        close = getattr(local_currents, "close", None)
        if callable(close):
            close()

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
    engine.move_bottle_live = _move_bottle_live_safe
