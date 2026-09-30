import json
import os
import subprocess
import sys

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Message in a Bottle Current Worker", version="0.2.0")


@app.get("/health")
def health():
    return {"ok": True, "service": "current-worker", "mode": "subprocess"}


@app.get("/current")
def current(lat: float, lon: float, date: str):
    env = os.environ.copy()
    env["MIAB_COPERNICUS_CHILD"] = "1"

    try:
        result = subprocess.run(
            [
                sys.executable,
                "copernicus_lookup.py",
                str(lat),
                str(lon),
                str(date),
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
            env=env,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Copernicus child failed: {exc}") from exc

    payload_line = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload_line = line
            break

    if payload_line is None:
        raise HTTPException(
            status_code=500,
            detail="Copernicus child returned no JSON result. " + result.stderr[-500:],
        )

    payload = json.loads(payload_line)

    # Convert the one-hour movement back into current components expected by
    # the main API. The child computes displacement from u/v, so we recover
    # u/v from that displacement deterministically.
    import math

    seconds = 3600.0
    north_m = (float(payload["latitude"]) - lat) * 111320.0
    east_m = (
        float(payload["longitude"]) - lon
    ) * (111320.0 * math.cos(math.radians(lat)))

    return {
        "u": east_m / seconds,
        "v": north_m / seconds,
    }
