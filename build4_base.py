from datetime import datetime, timezone

from fastapi import Body, HTTPException

import auth_support
import bottle_engine as engine
import storage

try:
    import sitecustomize as legacy_support
except Exception:
    legacy_support = None

REVIEW_USERNAME = "vicissitudereview"


def _init_build4_tables():
    if not storage.database_enabled():
        raise RuntimeError("Database is required for moderation features.")

    auth_support.init_auth_db()

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS content_reports (
                    id BIGSERIAL PRIMARY KEY,
                    reporter_account_id TEXT NOT NULL,
                    reported_account_id TEXT NOT NULL,
                    bottle_id TEXT,
                    message_text TEXT,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS content_reports_reporter_idx
                ON content_reports(reporter_account_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS content_reports_reported_idx
                ON content_reports(reported_account_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_blocks (
                    blocker_account_id TEXT NOT NULL,
                    blocked_account_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (blocker_account_id, blocked_account_id)
                )
                """
            )


def _account_from_token(token):
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=400, detail="auth_token is required.")

    account_id = auth_support.verify_session(token)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.")
    return account_id


def _is_review_account(account_id):
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM users
                WHERE account_id = %s
                  AND username_normalized = %s
                LIMIT 1
                """,
                (account_id, REVIEW_USERNAME),
            )
            return cur.fetchone() is not None


def is_blocked(blocker_account_id, blocked_account_id):
    if not storage.database_enabled():
        return False

    _init_build4_tables()
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM user_blocks
                WHERE blocker_account_id = %s
                  AND blocked_account_id = %s
                LIMIT 1
                """,
                (blocker_account_id, blocked_account_id),
            )
            return cur.fetchone() is not None


def _eligible_bottle_for(account_id):
    if legacy_support is None:
        return None, None

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for found_account_id, bottle in legacy_support._all_stored_bottles(
        exclude_account_id=account_id
    ):
        if is_blocked(account_id, found_account_id):
            continue
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

        return found_account_id, bottle

    return None, None


def _delete_account(account_id):
    _init_build4_tables()

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM content_reports WHERE reporter_account_id = %s OR reported_account_id = %s",
                (account_id, account_id),
            )
            cur.execute(
                "DELETE FROM user_blocks WHERE blocker_account_id = %s OR blocked_account_id = %s",
                (account_id, account_id),
            )
            cur.execute("DELETE FROM bottles WHERE account_id = %s", (account_id,))
            cur.execute("DELETE FROM entitlements WHERE account_id = %s", (account_id,))
            cur.execute(
                "DELETE FROM apple_purchase_transactions WHERE account_id = %s",
                (account_id,),
            )
            cur.execute("DELETE FROM account_sessions WHERE account_id = %s", (account_id,))
            cur.execute("DELETE FROM users WHERE account_id = %s", (account_id,))

    return {"ok": True, "deleted": True}


def _report_content(reporter_account_id, payload):
    _init_build4_tables()

    reported_account_id = payload.get("reported_account_id")
    bottle_id = payload.get("bottle_id")
    message = payload.get("message")
    reason = payload.get("reason") or "user_report"

    if not isinstance(reported_account_id, str) or not reported_account_id:
        raise HTTPException(status_code=400, detail="reported_account_id is required.")
    if reported_account_id == reporter_account_id:
        raise HTTPException(status_code=400, detail="You cannot report your own account.")

    if not isinstance(message, str):
        message = ""
    message = message[:4000]

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_reports (
                    reporter_account_id,
                    reported_account_id,
                    bottle_id,
                    message_text,
                    reason
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    reporter_account_id,
                    reported_account_id,
                    bottle_id if isinstance(bottle_id, str) else None,
                    message,
                    str(reason)[:200],
                ),
            )
            report_id = cur.fetchone()[0]

    return {"ok": True, "reported": True, "report_id": report_id}


def _block_user(blocker_account_id, payload):
    _init_build4_tables()

    blocked_account_id = payload.get("blocked_account_id")
    if not isinstance(blocked_account_id, str) or not blocked_account_id:
        raise HTTPException(status_code=400, detail="blocked_account_id is required.")
    if blocked_account_id == blocker_account_id:
        raise HTTPException(status_code=400, detail="You cannot block your own account.")

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_blocks (blocker_account_id, blocked_account_id)
                VALUES (%s, %s)
                ON CONFLICT (blocker_account_id, blocked_account_id)
                DO NOTHING
                """,
                (blocker_account_id, blocked_account_id),
            )

    return {"ok": True, "blocked": True}


def _find_beach_bottle(account_id):
    # Apple review fast path: the dedicated review account receives a
    # deterministic seeded bottle immediately instead of scanning every
    # stored bottle first. Normal accounts still use the ordinary path.
    if _is_review_account(account_id):
        if legacy_support is None:
            raise HTTPException(status_code=503, detail="Review bottle helper is unavailable.")
        found_account_id, bottle = legacy_support._create_test_beach_bottle()
    else:
        _init_build4_tables()
        found_account_id, bottle = _eligible_bottle_for(account_id)

    if bottle is None:
        return {"bottle_found": False}

    return {
        "bottle_found": True,
        "found_account_id": found_account_id,
        "bottle_id": bottle.get("bottle_id"),
        "status": bottle.get("status", "ashore"),
        "total_miles_traveled": round(
            float(bottle.get("total_miles_traveled", 0.0) or 0.0), 2
        ),
        "journey_areas": list(bottle.get("journey_areas", [])),
    }


def install_build4_routes(app):
    existing = {getattr(route, "path", None) for route in getattr(app, "routes", [])}

    if "/api/account/delete" not in existing:
        @app.post("/api/account/delete")
        def account_delete(payload: dict = Body(...)):
            account_id = _account_from_token(payload.get("auth_token"))
            return _delete_account(account_id)

    if "/api/moderation/report" not in existing:
        @app.post("/api/moderation/report")
        def moderation_report(payload: dict = Body(...)):
            account_id = _account_from_token(payload.get("auth_token"))
            return _report_content(account_id, payload)

    if "/api/moderation/block" not in existing:
        @app.post("/api/moderation/block")
        def moderation_block(payload: dict = Body(...)):
            account_id = _account_from_token(payload.get("auth_token"))
            return _block_user(account_id, payload)

    if "/api/beach-walk/find" not in existing:
        @app.post("/api/beach-walk/find")
        def beach_walk_find(payload: dict = Body(...)):
            account_id = _account_from_token(payload.get("auth_token"))
            finder_id = payload.get("finder_id")
            if not isinstance(finder_id, str) or not finder_id:
                raise HTTPException(status_code=400, detail="finder_id is required.")
            return _find_beach_bottle(account_id)


try:
    from fastapi import FastAPI

    _previous_fastapi_init = FastAPI.__init__

    def _fastapi_init_with_build4(self, *args, **kwargs):
        _previous_fastapi_init(self, *args, **kwargs)
        try:
            install_build4_routes(self)
        except Exception as exc:
            print(f"Build 4 route install skipped: {exc}")

    FastAPI.__init__ = _fastapi_init_with_build4
except Exception as exc:
    print(f"Build 4 startup patch skipped: {exc}")
