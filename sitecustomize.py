import json
import os
import subprocess
import sys

if os.environ.get("MIAB_COPERNICUS_CHILD") == "1":
    engine = None
else:
    try:
        import bottle_engine as engine
    except Exception:
        engine = None


def _move_bottle_live_isolated(lat, lon, date, hours=6):
    env = os.environ.copy()
    env["MIAB_COPERNICUS_CHILD"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "copernicus_lookup.py",
            str(lat),
            str(lon),
            str(date),
            str(hours),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
        env=env,
    )

    payload_line = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload_line = line
            break

    if payload_line is None:
        raise RuntimeError(
            "Copernicus worker returned no movement result. "
            + result.stderr[-500:]
        )

    payload = json.loads(payload_line)
    return (
        float(payload["latitude"]),
        float(payload["longitude"]),
        float(payload["miles"]),
    )


if engine is not None:
    engine.move_bottle_live = _move_bottle_live_isolated
