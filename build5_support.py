import re
from datetime import datetime, timezone

from fastapi import Body, HTTPException

import auth_support
import bottle_engine as engine
import build4_support
import storage

SUPPORT_EMAIL = "support@nobudgetinternational.com"
SAFETY_POLICY_VERSION = "2026-10-05"

# High-confidence categories only. The filter is intentionally server-side so
# it cannot be bypassed by an older client build.
_PROHIBITED_PATTERNS = [
    re.compile(r"\b(kill|murder|shoot|stab)\s+(you|him|her|them|yourself)\b", re.I),
    re.compile(r"\b(i|we)\s+(will|am going to|gonna)\s+(kill|murder|shoot|stab|hurt)\b", re.I),
    re.compile(r"\b(go|you should)\s+(kill|hurt)\s+yourself\b", re.I),
    re.compile(r"\b(child|kid|minor|underage)\b.{0,40}\b(sex|sexual|nude|naked|porn)\b", re.I),
    re.compile(r"\b(rape|raping|rapist)\b", re.I),
    re.compile(r"\b(nazi|kkk)\b.{0,30}\b(join|support|hail|glory)\b", re.I),
]


def _normalize_text(value):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    text = text.replace("0", "o").replace("1", "i").replace("3", "e").replace("@", "a")
    return re.sub(r"\s+", " ", text)


def _validate_user_content(value, field_name="message"):
    text = _normalize_text(value)
    if not text:
        return value

    if len(text) > 4000:
        raise HTTPException(status_code=400, detail=f"{field_name} is too long.")

    for pattern in _PROHIBITED_PATTERNS:
        if pattern.search(text):
            raise HTTPException(
                status_code=400,
                detail="This message cannot be sent because it violates Vicissitude safety rules.",
            )

    return value


def _init_build5_tables():
    if not storage.database_enabled():
        return

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS suspended_accounts (
                    account_id TEXT PRIMARY KEY,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    reviewed_at TIMESTAMPTZ,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_actions (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT,
                    bottle_id TEXT,
                    action TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )


def is_suspended(account_id):
    if not account_id or not storage.database_enabled():
        return False

    _init_build5_tables()
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM suspended_accounts
                WHERE account_id = %s AND active = TRUE
                LIMIT 1
                """,
                (account_id,),
            )
            return cur.fetchone() is not None


def _suspend_account(account_id, bottle_id=None, reason="user_report"):
    if not account_id or not storage.database_enabled():
        return

    _init_build5_tables()
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO suspended_accounts (account_id, reason, active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (account_id)
                DO UPDATE SET reason = EXCLUDED.reason, active = TRUE, reviewed_at = NULL
                """,
                (account_id, reason),
            )
            cur.execute(
                """
                INSERT INTO moderation_actions (account_id, bottle_id, action, reason)
                VALUES (%s, %s, 'suspend_account', %s)
                """,
                (account_id, bottle_id, reason),
            )


def _quarantine_bottle(account_id, bottle_id=None, reason="user_report"):
    try:
        bottle = storage.load_bottle(account_id)
    except Exception as exc:
        print(f"Build 5 quarantine load failed: {exc}")
        return

    if bottle is None:
        return

    if bottle_id and bottle.get("bottle_id") != bottle_id:
        return

    bottle["finder_eligible"] = False
    bottle["moderated_removed"] = True
    bottle["moderated_reason"] = reason
    bottle["moderated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    bottle["message"] = ""

    try:
        storage.save_bottle(bottle)
    except Exception as exc:
        print(f"Build 5 quarantine save failed: {exc}")

    if storage.database_enabled():
        _init_build5_tables()
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO moderation_actions (account_id, bottle_id, action, reason)
                    VALUES (%s, %s, 'remove_content', %s)
                    """,
                    (account_id, bottle_id, reason),
                )


# --- Server-side content filtering ---
_original_create_bottle = engine.create_bottle
_original_reply_to_sender = engine.reply_to_sender


def _safe_create_bottle(*args, **kwargs):
    message = kwargs.get("message")
    if message is None and len(args) >= 5:
        message = args[4]
    _validate_user_content(message, "message")

    sender_id = kwargs.get("sender_id")
    if sender_id is None and len(args) >= 4:
        sender_id = args[3]
    if sender_id and is_suspended(sender_id):
        raise HTTPException(status_code=403, detail="This account is suspended from sending content.")

    return _original_create_bottle(*args, **kwargs)


def _safe_reply_to_sender(bottle, reply_message, *args, **kwargs):
    _validate_user_content(reply_message, "reply_message")
    return _original_reply_to_sender(bottle, reply_message, *args, **kwargs)


engine.create_bottle = _safe_create_bottle
engine.reply_to_sender = _safe_reply_to_sender


# --- Extend Build 4 moderation behavior ---
_original_report_content = build4_support._report_content
_original_is_blocked = build4_support.is_blocked


def _report_content_and_remove(reporter_account_id, payload):
    result = _original_report_content(reporter_account_id, payload)

    reported_account_id = payload.get("reported_account_id")
    bottle_id = payload.get("bottle_id")
    reason = str(payload.get("reason") or "user_report")[:200]

    # Apple Guideline 1.2: remove reported content from distribution immediately
    # and eject the offending account pending moderation review.
    _quarantine_bottle(reported_account_id, bottle_id, reason)
    _suspend_account(reported_account_id, bottle_id, reason)

    result["content_removed"] = True
    result["account_suspended"] = True
    return result


def _blocked_or_suspended(blocker_account_id, blocked_account_id):
    if is_suspended(blocked_account_id):
        return True
    return _original_is_blocked(blocker_account_id, blocked_account_id)


build4_support._report_content = _report_content_and_remove
build4_support.is_blocked = _blocked_or_suspended


# Make an existing login/session unusable once the account is suspended.
_original_verify_session = auth_support.verify_session


def _verify_session_not_suspended(token):
    account_id = _original_verify_session(token)
    if account_id and is_suspended(account_id):
        return None
    return account_id


auth_support.verify_session = _verify_session_not_suspended


def install_build5_routes(app):
    existing = {getattr(route, "path", None) for route in getattr(app, "routes", [])}

    if "/api/safety/policy" not in existing:
        @app.get("/api/safety/policy")
        def safety_policy():
            return {
                "minimum_age": 18,
                "terms_required": True,
                "zero_tolerance": True,
                "report_review_window_hours": 24,
                "support_email": SUPPORT_EMAIL,
                "policy_version": SAFETY_POLICY_VERSION,
                "statement": (
                    "Vicissitude has zero tolerance for objectionable content or abusive users. "
                    "Users can report content and block users. Reported content is removed from "
                    "distribution immediately and reports are reviewed within 24 hours."
                ),
            }


# build4_support has already wrapped FastAPI.__init__ before this module loads.
# Wrap the resulting initializer so Build 5 routes are present on every app.
try:
    from fastapi import FastAPI

    _previous_fastapi_init = FastAPI.__init__

    def _fastapi_init_with_build5(self, *args, **kwargs):
        _previous_fastapi_init(self, *args, **kwargs)
        try:
            _init_build5_tables()
            install_build5_routes(self)
        except Exception as exc:
            print(f"Build 5 route install skipped: {exc}")

    FastAPI.__init__ = _fastapi_init_with_build5
except Exception as exc:
    print(f"Build 5 startup patch skipped: {exc}")
