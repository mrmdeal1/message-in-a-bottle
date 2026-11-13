import json
import math
import os
import subprocess
import sys
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Message in a Bottle Current Worker", version="0.3.2")

# Each lookup can launch a memory-heavy Copernicus subprocess. Render's
# 512 MiB worker must never run more than one at once.
_heavy_job_slot = BoundedSemaphore(value=1)


def _run_single_heavy_job(*args, **kwargs):
    if not _heavy_job_slot.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Ocean-current calculation is busy. Retry shortly.",
            headers={"Retry-After": "15"},
        )
    try:
        return subprocess.run(*args, **kwargs)
    finally:
        _heavy_job_slot.release()



@app.get("/health")
def health():
    return {"ok": True, "service": "current-worker", "mode": "subprocess"}


@app.get("/current")
def current(lat: float, lon: float, date: str):
    env = os.environ.copy()
    env["MIAB_COPERNICUS_CHILD"] = "1"

    try:
        result = _run_single_heavy_job(
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
    except HTTPException:
        raise
    except subprocess.CalledProcessError as exc:
        if "No usable finite ocean current" in (exc.stderr or ""):
            raise HTTPException(
                status_code=422,
                detail="No measured ocean current available near these coordinates.",
            ) from exc
        raise HTTPException(status_code=502, detail="Copernicus lookup failed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Copernicus lookup timed out.") from exc
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

    try:
        payload = json.loads(payload_line)
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
        if not all(math.isfinite(value) for value in (latitude, longitude, lat, lon)):
            raise ValueError("Non-finite ocean-current position")
        seconds = 3600.0
        north_m = (latitude - lat) * 111320.0
        east_m = (longitude - lon) * (111320.0 * math.cos(math.radians(lat)))
        u, v = east_m / seconds, north_m / seconds
        if not all(math.isfinite(value) for value in (u, v)):
            raise ValueError("Non-finite ocean-current vector")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid Copernicus current result.") from exc

    return {"u": u, "v": v}


@app.post("/annual")
def annual(bottle: dict):
    env = os.environ.copy()
    env["MIAB_COPERNICUS_CHILD"] = "1"

    try:
        result = _run_single_heavy_job(
            [sys.executable, "annual_simulator_fast.py"],
            input=json.dumps(bottle),
            capture_output=True,
            text=True,
            timeout=360,
            check=True,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail="Annual simulation exceeded the worker time limit.",
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc))[-1500:]
        raise HTTPException(
            status_code=500,
            detail=f"Annual simulation failed: {detail}",
        ) from exc

    payload_line = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload_line = line
            break

    if payload_line is None:
        raise HTTPException(
            status_code=500,
            detail="Annual simulator returned no JSON result.",
        )

    return json.loads(payload_line)
