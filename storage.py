import json
import os
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

SAVE_FILE = "bottle_state.json"


def database_enabled():
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    return psycopg.connect(os.environ["DATABASE_URL"])


def init_db():
    if not database_enabled():
        return False

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bottles (
                    account_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    bottle_id TEXT PRIMARY KEY,
                    state JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (account_id, month_key)
                )
                """
            )
    return True


def _launch_month_from_bottle(bottle):
    launch_month = bottle.get("launch_month_key")
    if launch_month:
        return launch_month

    for event in bottle.get("journey_history", []):
        if event.get("event") == "launched" and event.get("time"):
            launch_month = datetime.fromisoformat(event["time"]).strftime("%Y-%m")
            bottle["launch_month_key"] = launch_month
            return launch_month

    value = bottle.get("current_time")
    if value:
        launch_month = datetime.fromisoformat(value).strftime("%Y-%m")
    else:
        launch_month = datetime.now(timezone.utc).strftime("%Y-%m")

    bottle["launch_month_key"] = launch_month
    return launch_month


def month_key_for(bottle):
    storage_month = bottle.get("_storage_month_key")
    if storage_month:
        return storage_month
    return _launch_month_from_bottle(bottle)


def account_id_for(bottle):
    return bottle.get("_storage_account_id") or bottle.get("sender_id") or "demo-account"


def attach_account(bottle, account_id, month_key=None):
    bottle["_storage_account_id"] = account_id
    if month_key:
        bottle["_storage_month_key"] = month_key
        bottle.setdefault("launch_month_key", month_key)
    return bottle


def save_bottle(bottle):
    if not database_enabled():
        with open(SAVE_FILE, "w") as f:
            json.dump(bottle, f, indent=2)
        return

    account_id = account_id_for(bottle)
    month_key = month_key_for(bottle)

    state = dict(bottle)
    state.pop("_storage_account_id", None)
    state.pop("_storage_month_key", None)
    state.setdefault("launch_month_key", month_key)

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bottles
                    (account_id, month_key, bottle_id, state, updated_at)
                VALUES
                    (%s, %s, %s, %s, NOW())
                ON CONFLICT (account_id, month_key)
                DO UPDATE SET
                    bottle_id = EXCLUDED.bottle_id,
                    state = EXCLUDED.state,
                    updated_at = NOW()
                """,
                (
                    account_id,
                    month_key,
                    bottle["bottle_id"],
                    Jsonb(state),
                ),
            )


def load_bottle(account_id=None, month_key=None):
    if not database_enabled():
        if not os.path.exists(SAVE_FILE):
            return None

        with open(SAVE_FILE, "r") as f:
            return json.load(f)

    account_id = account_id or "demo-account"

    with _connect() as conn:
        with conn.cursor() as cur:
            if month_key is None:
                cur.execute(
                    """
                    SELECT state, month_key
                    FROM bottles
                    WHERE account_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (account_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT state, month_key
                    FROM bottles
                    WHERE account_id = %s
                      AND month_key = %s
                    """,
                    (account_id, month_key),
                )
            row = cur.fetchone()

    if row is None:
        return None

    bottle = row[0]
    stored_month_key = row[1]
    attach_account(bottle, account_id, stored_month_key)
    return bottle


def account_has_bottle(account_id, month_key=None):
    if not database_enabled():
        return os.path.exists(SAVE_FILE)

    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM bottles
                WHERE account_id = %s
                  AND month_key = %s
                LIMIT 1
                """,
                (account_id, month_key),
            )
            return cur.fetchone() is not None
