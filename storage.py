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


def month_key_for(bottle):
    value = bottle.get("current_time")
    if value:
        return datetime.fromisoformat(value).strftime("%Y-%m")
    return datetime.now(timezone.utc).strftime("%Y-%m")


def account_id_for(bottle):
    return bottle.get("_storage_account_id") or bottle.get("sender_id") or "demo-account"


def attach_account(bottle, account_id):
    bottle["_storage_account_id"] = account_id
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

    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state
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
    attach_account(bottle, account_id)
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
