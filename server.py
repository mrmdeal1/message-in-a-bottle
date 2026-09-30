from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import bottle_engine as engine
import storage


app = FastAPI(title="Message in a Bottle API", version="0.1.0")

if storage.database_enabled():
    storage.init_db()

engine.save_bottle = storage.save_bottle


class LaunchRequest(BaseModel):
    latitude: float
    longitude: float
    account_id: str | None = None
    sender_id: str | None = None
    message: str | None = None
    start_time: str | None = None


class ActionRequest(BaseModel):
    account_id: str | None = None
    event_time: str | None = None
    finder_id: str | None = None
    reply_message: str | None = None


class AdvanceRequest(BaseModel):
    account_id: str | None = None
    total_hours: int = 24
    step_hours: int = 6


def account_for(value):
    return value or "demo-account"


def load_for_account(account_id):
    bottle = storage.load_bottle(account_id)
    if bottle is None:
        return None

    storage.attach_account(bottle, account_id)
    return bottle


def demo_copernicus_date(journey_time):
    window_start = datetime(2026, 9, 20)
    window_days = 11
    journey_dt = datetime.fromisoformat(journey_time).replace(tzinfo=None)
    offset = (journey_dt.date() - window_start.date()).days % window_days
    return (window_start + timedelta(days=offset)).strftime("%Y-%m-%d")


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "message-in-a-bottle",
        "version": "0.1.0",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "message-in-a-bottle",
        "storage": "postgres" if storage.database_enabled() else "local",
    }


@app.post("/api/test/launch-demo")
def launch_demo():
    account_id = "demo-account"

    existing = load_for_account(account_id)
    if existing is not None:
        return {
            "created": False,
            "message": "Demo bottle already exists.",
            "bottle_id": existing["bottle_id"],
            "status": existing["status"],
            "current_time": existing.get("current_time"),
            "eligible_after": existing.get("eligible_after"),
            "journey_areas": existing.get("journey_areas", []),
        }

    start_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    bottle = engine.create_bottle(
        29.0,
        -88.0,
        start_time=start_time,
        sender_id="mickey",
        message="My first bottle is going into the Gulf of Mexico.",
    )
    storage.attach_account(bottle, account_id)
    storage.save_bottle(bottle)

    return {
        "created": True,
        "bottle_id": bottle["bottle_id"],
        "status": bottle["status"],
        "current_time": bottle.get("current_time"),
        "eligible_after": bottle["eligible_after"],
        "journey_areas": bottle["journey_areas"],
    }


@app.post("/api/test/reset-demo")
def reset_demo():
    account_id = "demo-account"
    start_time = "2026-09-20T00:00:00+00:00"

    bottle = engine.create_bottle(
        29.0,
        -88.0,
        start_time=start_time,
        sender_id="mickey",
        message="My first bottle is going into the Gulf of Mexico.",
    )
    storage.attach_account(bottle, account_id)
    storage.save_bottle(bottle)

    return {
        "reset": True,
        "bottle_id": bottle["bottle_id"],
        "status": bottle["status"],
        "current_time": bottle.get("current_time"),
        "eligible_after": bottle["eligible_after"],
        "journey_areas": bottle["journey_areas"],
    }


@app.post("/api/test/advance-demo")
def advance_demo():
    account_id = "demo-account"
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(
            status_code=404,
            detail="Demo bottle does not exist. Run /api/test/launch-demo first.",
        )

    original_move = engine.move_bottle_live
    start_history_len = len(bottle.get("journey_history", []))
    days_advanced = 0
    last_lookup_date = None
    stop_event = None
    meaningful_events = {
        "storm_encountered",
        "washed_ashore",
        "lost_at_sea",
        "found",
        "opened",
        "thrown_back",
    }

    try:
        for _ in range(30):
            if bottle.get("opened") or bottle.get("status") != "drifting":
                break

            lookup_date = demo_copernicus_date(bottle["current_time"])
            last_lookup_date = lookup_date

            def demo_move(lat, lon, _date, hours=6, lookup_date=lookup_date):
                return original_move(lat, lon, lookup_date, hours)

            engine.move_bottle_live = demo_move
            previous_history_len = len(bottle.get("journey_history", []))

            bottle = engine.advance_bottle(
                bottle,
                total_hours=24,
                step_hours=24,
            )
            days_advanced += 1
            storage.save_bottle(bottle)

            new_events = bottle.get("journey_history", [])[previous_history_len:]
            for event in new_events:
                if event.get("event") in meaningful_events:
                    stop_event = event.get("event")
                    break

            if stop_event or bottle.get("status") != "drifting" or bottle.get("opened"):
                break
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Advance failed: {exc}") from exc
    finally:
        engine.move_bottle_live = original_move

    new_history = bottle.get("journey_history", [])[start_history_len:]
    if stop_event is None:
        for event in new_history:
            if event.get("event") in meaningful_events:
                stop_event = event.get("event")
                break

    return {
        "bottle_id": bottle["bottle_id"],
        "status": bottle["status"],
        "current_time": bottle.get("current_time"),
        "days_advanced": days_advanced,
        "stop_event": stop_event,
        "copernicus_lookup_date": last_lookup_date,
        "total_miles_traveled": round(bottle.get("total_miles_traveled", 0.0), 2),
        "journey_areas": bottle.get("journey_areas", []),
    }


@app.post("/api/bottle/launch")
def launch(req: LaunchRequest):
    account_id = account_for(req.account_id or req.sender_id)

    if storage.database_enabled():
        storage.init_db()

    if storage.account_has_bottle(account_id):
        raise HTTPException(
            status_code=409,
            detail="This account has already used its bottle for this calendar month.",
        )

    start_time = (
        req.start_time
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )

    bottle = engine.create_bottle(
        req.latitude,
        req.longitude,
        start_time=start_time,
        sender_id=req.sender_id,
        message=req.message,
    )

    storage.attach_account(bottle, account_id)
    storage.save_bottle(bottle)

    return {
        "bottle_id": bottle["bottle_id"],
        "status": bottle["status"],
        "eligible_after": bottle["eligible_after"],
        "journey_areas": bottle["journey_areas"],
    }


@app.get("/api/bottle")
def current_bottle(account_id: str = "demo-account"):
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    return engine.finder_view(bottle)


@app.post("/api/bottle/advance")
def advance(req: AdvanceRequest):
    account_id = account_for(req.account_id)
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    if req.total_hours <= 0 or req.step_hours <= 0:
        raise HTTPException(
            status_code=400,
            detail="total_hours and step_hours must be greater than zero.",
        )

    if req.total_hours % req.step_hours != 0:
        raise HTTPException(
            status_code=400,
            detail="total_hours must be evenly divisible by step_hours.",
        )

    try:
        result = engine.advance_bottle(
            bottle,
            total_hours=req.total_hours,
            step_hours=req.step_hours,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Advance failed: {exc}") from exc

    storage.save_bottle(result)

    return {
        "bottle_id": result["bottle_id"],
        "status": result["status"],
        "current_time": result.get("current_time"),
        "total_miles_traveled": round(result.get("total_miles_traveled", 0.0), 2),
        "journey_areas": result.get("journey_areas", []),
    }


@app.post("/api/bottle/encounter")
def encounter(req: ActionRequest):
    account_id = account_for(req.account_id)
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    result = engine.encounter_bottle(
        bottle,
        finder_id=req.finder_id,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return engine.finder_view(result)


@app.post("/api/bottle/open")
def open_bottle(req: ActionRequest):
    account_id = account_for(req.account_id)
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    result = engine.open_bottle(
        bottle,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return engine.finder_view(result)


@app.post("/api/bottle/throw-back")
def throw_back(req: ActionRequest):
    account_id = account_for(req.account_id)
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    result = engine.throw_back(
        bottle,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return {
        "status": result["status"],
        "bottle_id": result["bottle_id"],
    }


@app.post("/api/bottle/reply")
def reply(req: ActionRequest):
    if req.reply_message is None:
        raise HTTPException(
            status_code=400,
            detail="reply_message is required.",
        )

    account_id = account_for(req.account_id)
    bottle = load_for_account(account_id)

    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists for this account.")

    result = engine.reply_to_sender(
        bottle,
        req.reply_message,
        event_time=req.event_time,
    )

    if isinstance(result, str) and result != "Reply sent to original sender.":
        raise HTTPException(status_code=409, detail=result)

    return {
        "result": result,
        "view": engine.finder_view(bottle),
    }
