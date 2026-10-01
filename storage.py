import json
import os
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

SAVE_FILE = "bottle_state.json"
ENTITLEMENT_FILE = "entitlements_state.json"


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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entitlements (
                    account_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    consumed_at TIMESTAMPTZ,
                    payment_provider TEXT,
                    payment_reference TEXT,
                    PRIMARY KEY (account_id, month_key)
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
        account_id = account_id_for(bottle)
        month_key = month_key_for(bottle)

        data = {}
        if os.path.exists(SAVE_FILE):
            try:
                with open(SAVE_FILE, "r") as f:
                    existing = json.load(f)

                if (
                    isinstance(existing, dict)
                    and "bottles" in existing
                    and isinstance(existing["bottles"], dict)
                ):
                    data = existing
                elif isinstance(existing, dict) and existing.get("bottle_id"):
                    old_account = account_id_for(existing)
                    old_month = month_key_for(existing)
                    data = {
                        "bottles": {
                            f"{old_account}|{old_month}": existing
                        }
                    }
            except Exception:
                data = {}

        data.setdefault("bottles", {})
        data["bottles"][f"{account_id}|{month_key}"] = bottle

        with open(SAVE_FILE, "w") as f:
            json.dump(data, f, indent=2)
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
            data = json.load(f)

        if isinstance(data, dict) and "bottles" in data:
            account_id = account_id or "demo-account"

            if month_key is not None:
                bottle = data["bottles"].get(f"{account_id}|{month_key}")
                if bottle is None:
                    return None
                attach_account(bottle, account_id, month_key)
                return bottle

            matches = []
            for key, bottle in data["bottles"].items():
                stored_account, stored_month = key.split("|", 1)
                if stored_account == account_id:
                    matches.append((stored_month, bottle))

            if not matches:
                return None

            stored_month, bottle = sorted(matches)[-1]
            attach_account(bottle, account_id, stored_month)
            return bottle

        if isinstance(data, dict) and data.get("bottle_id"):
            stored_account = account_id_for(data)
            stored_month = month_key_for(data)

            if account_id is not None and stored_account != account_id:
                return None

            if month_key is not None and stored_month != month_key:
                return None

            attach_account(data, stored_account, stored_month)
            return data

        return None

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
        if month_key is None:
            month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        return load_bottle(account_id, month_key) is not None

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

def grant_monthly_entitlement(
    account_id,
    month_key=None,
    payment_provider=None,
    payment_reference=None,
):
    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if not database_enabled():
        data = {}

        if os.path.exists(ENTITLEMENT_FILE):
            with open(ENTITLEMENT_FILE, "r") as f:
                data = json.load(f)

        key = f"{account_id}:{month_key}"

        if key not in data:
            data[key] = {
                "account_id": account_id,
                "month_key": month_key,
                "granted_at": datetime.now(timezone.utc).isoformat(),
                "consumed_at": None,
                "payment_provider": payment_provider,
                "payment_reference": payment_reference,
            }

            with open(ENTITLEMENT_FILE, "w") as f:
                json.dump(data, f, indent=2)

        return {
            "account_id": account_id,
            "month_key": month_key,
            "granted": True,
            "local_mode": True,
        }

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entitlements (
                    account_id,
                    month_key,
                    payment_provider,
                    payment_reference
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (account_id, month_key)
                DO NOTHING
                """,
                (
                    account_id,
                    month_key,
                    payment_provider,
                    payment_reference,
                ),
            )

    return {
        "account_id": account_id,
        "month_key": month_key,
        "granted": True,
    }


def entitlement_available(account_id, month_key=None):
    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if not database_enabled():
        if not os.path.exists(ENTITLEMENT_FILE):
            return False

        with open(ENTITLEMENT_FILE, "r") as f:
            data = json.load(f)

        key = f"{account_id}:{month_key}"
        record = data.get(key)

        return record is not None and record.get("consumed_at") is None

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT consumed_at
                FROM entitlements
                WHERE account_id = %s
                  AND month_key = %s
                """,
                (account_id, month_key),
            )
            row = cur.fetchone()

    return row is not None and row[0] is None


def consume_entitlement(account_id, month_key=None):
    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if not database_enabled():
        if not os.path.exists(ENTITLEMENT_FILE):
            return False

        with open(ENTITLEMENT_FILE, "r") as f:
            data = json.load(f)

        key = f"{account_id}:{month_key}"
        record = data.get(key)

        if record is None or record.get("consumed_at") is not None:
            return False

        record["consumed_at"] = datetime.now(timezone.utc).isoformat()

        with open(ENTITLEMENT_FILE, "w") as f:
            json.dump(data, f, indent=2)

        return True

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE entitlements
                SET consumed_at = NOW()
                WHERE account_id = %s
                  AND month_key = %s
                  AND consumed_at IS NULL
                RETURNING consumed_at
                """,
                (account_id, month_key),
            )
            row = cur.fetchone()

    return row is not None

