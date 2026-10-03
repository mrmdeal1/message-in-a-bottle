import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import Body, HTTPException
from psycopg.errors import UniqueViolation
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)

import storage

PBKDF2_ITERATIONS = 310000
SESSION_DAYS = 30

APPLE_BUNDLE_ID = os.getenv(
    "APPLE_BUNDLE_ID",
    "com.nobudgetinternational.messageinabottle",
)
APPLE_PRODUCT_ID = os.getenv(
    "APPLE_PRODUCT_ID",
    "message_in_a_bottle.monthly_bottle",
)
APPLE_ENVIRONMENT_NAME = os.getenv("APPLE_ENVIRONMENT", "sandbox").strip().lower()
APPLE_APP_ID_TEXT = os.getenv("APPLE_APP_ID", "").strip()
APPLE_ROOT_CERT_DIR = "apple_root_certs"


def _normalize_username(username):
    return username.strip().lower()


def _hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password, stored):
    try:
        algorithm, iterations_text, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _apple_environment():
    if APPLE_ENVIRONMENT_NAME in {"production", "prod"}:
        return Environment.PRODUCTION
    return Environment.SANDBOX


def _apple_app_id():
    if not APPLE_APP_ID_TEXT:
        return None
    try:
        return int(APPLE_APP_ID_TEXT)
    except ValueError as exc:
        raise RuntimeError("APPLE_APP_ID must be a numeric App Store app ID.") from exc


def _load_apple_root_certificates():
    cert_paths = [
        os.path.join(APPLE_ROOT_CERT_DIR, "AppleIncRootCertificate.cer"),
        os.path.join(APPLE_ROOT_CERT_DIR, "AppleRootCA-G2.cer"),
        os.path.join(APPLE_ROOT_CERT_DIR, "AppleRootCA-G3.cer"),
    ]

    certificates = []
    for cert_path in cert_paths:
        with open(cert_path, "rb") as f:
            certificates.append(f.read())
    return certificates


def _make_apple_verifier():
    environment = _apple_environment()
    app_id = _apple_app_id()

    if environment == Environment.PRODUCTION and app_id is None:
        raise RuntimeError(
            "APPLE_APP_ID is required when APPLE_ENVIRONMENT=production."
        )

    return SignedDataVerifier(
        _load_apple_root_certificates(),
        True,
        environment,
        APPLE_BUNDLE_ID,
        app_id,
    )


def init_auth_db():
    if not storage.database_enabled():
        raise RuntimeError("Database is required for account authentication.")

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    account_id TEXT PRIMARY KEY,
                    username_normalized TEXT NOT NULL UNIQUE,
                    display_username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES users(account_id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS account_sessions_account_idx
                ON account_sessions(account_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS apple_purchase_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES users(account_id) ON DELETE CASCADE,
                    month_key TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS apple_purchase_account_month_idx
                ON apple_purchase_transactions(account_id, month_key)
                """
            )
    return True


def _validate_credentials(username, password):
    display_username = username.strip()
    normalized = _normalize_username(username)

    if len(display_username) < 3 or len(display_username) > 40:
        raise HTTPException(
            status_code=400,
            detail="Username must be between 3 and 40 characters.",
        )

    if len(password) < 8 or len(password) > 128:
        raise HTTPException(
            status_code=400,
            detail="Password must be between 8 and 128 characters.",
        )

    return display_username, normalized


def _issue_session(account_id):
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM account_sessions
                WHERE expires_at <= NOW()
                """
            )
            cur.execute(
                """
                INSERT INTO account_sessions
                    (token_hash, account_id, expires_at)
                VALUES
                    (%s, %s, %s)
                """,
                (_token_hash(token), account_id, expires_at),
            )

    return token, expires_at


def register_account(username, password):
    init_auth_db()
    display_username, normalized = _validate_credentials(username, password)
    account_id = str(uuid.uuid4())
    password_hash = _hash_password(password)

    try:
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users
                        (account_id, username_normalized, display_username, password_hash)
                    VALUES
                        (%s, %s, %s, %s)
                    """,
                    (account_id, normalized, display_username, password_hash),
                )
    except UniqueViolation as exc:
        raise HTTPException(
            status_code=409,
            detail="That username is already registered.",
        ) from exc

    token, expires_at = _issue_session(account_id)
    return {
        "ok": True,
        "account_id": account_id,
        "username": display_username,
        "auth_token": token,
        "expires_at": expires_at.replace(microsecond=0).isoformat(),
    }


def login_account(username, password):
    init_auth_db()
    normalized = _normalize_username(username)

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, display_username, password_hash
                FROM users
                WHERE username_normalized = %s
                """,
                (normalized,),
            )
            row = cur.fetchone()

    if row is None or not _verify_password(password, row[2]):
        raise HTTPException(
            status_code=401,
            detail="Username or password is incorrect.",
        )

    token, expires_at = _issue_session(row[0])
    return {
        "ok": True,
        "account_id": row[0],
        "username": row[1],
        "auth_token": token,
        "expires_at": expires_at.replace(microsecond=0).isoformat(),
    }


def verify_session(token):
    if not token:
        return None

    init_auth_db()
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id
                FROM account_sessions
                WHERE token_hash = %s
                  AND expires_at > NOW()
                """,
                (_token_hash(token),),
            )
            row = cur.fetchone()

    return row[0] if row else None


def _launch_time(state):
    for event in state.get("journey_history", []):
        if event.get("event") == "launched" and event.get("time"):
            return event.get("time")
    return state.get("current_time")


def _history_outcome(state):
    status = str(state.get("status") or "drifting").lower()
    if state.get("opened") or "opened" in status or "journey_ended" in status:
        return "Opened"
    if state.get("destroyed") or "lost" in status or "destroyed" in status:
        return "Lost"
    if "ashore" in status:
        return "Ashore"
    return "Drifting"


def account_history(account_id):
    if not storage.database_enabled():
        return []

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.month_key,
                    b.bottle_id,
                    b.state,
                    e.granted_at
                FROM bottles b
                LEFT JOIN entitlements e
                  ON e.account_id = b.account_id
                 AND e.month_key = b.month_key
                WHERE b.account_id = %s
                ORDER BY b.month_key DESC, b.updated_at DESC
                """,
                (account_id,),
            )
            rows = cur.fetchall()

    history = []
    for month_key, bottle_id, state, granted_at in rows:
        state = dict(state or {})
        areas = list(state.get("journey_areas") or [])
        history.append(
            {
                "bottle_id": bottle_id,
                "month_key": month_key,
                "purchased_at": granted_at.replace(microsecond=0).isoformat() if granted_at else None,
                "thrown_at": _launch_time(state),
                "location": areas[-1] if areas else "Open Ocean",
                "miles": round(float(state.get("total_miles_traveled", 0.0) or 0.0), 2),
                "outcome": _history_outcome(state),
            }
        )

    return history


def purchase_status(account_id):
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    has_bottle = storage.account_has_bottle(account_id, month_key)
    entitlement_available = storage.entitlement_available(account_id, month_key)

    return {
        "ok": True,
        "account_id": account_id,
        "month_key": month_key,
        "has_bottle": has_bottle,
        "entitlement_available": entitlement_available,
        "can_purchase": not has_bottle and not entitlement_available,
    }


def _grant_verified_apple_purchase(account_id, transaction_id, product_id):
    init_auth_db()

    month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if storage.account_has_bottle(account_id, month_key):
        raise HTTPException(
            status_code=409,
            detail="This account has already used its bottle for this calendar month.",
        )

    if storage.entitlement_available(account_id, month_key):
        return {
            "ok": True,
            "granted": True,
            "already_available": True,
            "account_id": account_id,
            "month_key": month_key,
            "product_id": product_id,
        }

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, month_key, product_id
                FROM apple_purchase_transactions
                WHERE transaction_id = %s
                """,
                (transaction_id,),
            )
            existing = cur.fetchone()

            if existing is not None:
                existing_account, existing_month, existing_product = existing
                if (
                    existing_account == account_id
                    and existing_month == month_key
                    and existing_product == product_id
                ):
                    cur.execute(
                        """
                        SELECT consumed_at
                        FROM entitlements
                        WHERE account_id = %s
                          AND month_key = %s
                        """,
                        (account_id, month_key),
                    )
                    entitlement_row = cur.fetchone()
                    if entitlement_row is not None and entitlement_row[0] is None:
                        return {
                            "ok": True,
                            "granted": True,
                            "already_available": True,
                            "account_id": account_id,
                            "month_key": month_key,
                            "product_id": product_id,
                        }

                raise HTTPException(
                    status_code=409,
                    detail="This Apple transaction has already been used.",
                )

            cur.execute(
                """
                SELECT 1
                FROM entitlements
                WHERE account_id = %s
                  AND month_key = %s
                LIMIT 1
                """,
                (account_id, month_key),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="This account already has a bottle entitlement for this calendar month.",
                )

            cur.execute(
                """
                INSERT INTO apple_purchase_transactions
                    (transaction_id, account_id, month_key, product_id)
                VALUES
                    (%s, %s, %s, %s)
                """,
                (transaction_id, account_id, month_key, product_id),
            )

            cur.execute(
                """
                INSERT INTO entitlements (
                    account_id,
                    month_key,
                    payment_provider,
                    payment_reference
                )
                VALUES (%s, %s, 'apple', %s)
                """,
                (account_id, month_key, transaction_id),
            )

    return {
        "ok": True,
        "granted": True,
        "already_available": False,
        "account_id": account_id,
        "month_key": month_key,
        "product_id": product_id,
    }


def verify_and_grant_apple_purchase(auth_token, signed_transaction):
    account_id = verify_session(auth_token)
    if account_id is None:
        raise HTTPException(
            status_code=401,
            detail="Session is invalid or expired.",
        )

    try:
        verifier = _make_apple_verifier()
        transaction = verifier.verify_and_decode_signed_transaction(
            signed_transaction
        )
    except VerificationException as exc:
        raise HTTPException(
            status_code=400,
            detail="Apple transaction could not be verified.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Apple purchase verification is not configured correctly: {exc}",
        ) from exc

    if transaction.productId != APPLE_PRODUCT_ID:
        raise HTTPException(
            status_code=400,
            detail="Apple transaction is for the wrong product.",
        )

    if getattr(transaction, "revocationDate", None) is not None:
        raise HTTPException(
            status_code=409,
            detail="This Apple transaction has been revoked.",
        )

    transaction_id = getattr(transaction, "transactionId", None)
    if not transaction_id:
        raise HTTPException(
            status_code=400,
            detail="Apple transaction is missing a transaction ID.",
        )

    return _grant_verified_apple_purchase(
        account_id,
        str(transaction_id),
        transaction.productId,
    )


def install_auth_routes(app):
    existing = {getattr(route, "path", None) for route in getattr(app, "routes", [])}

    if "/api/account/register" not in existing:
        @app.post("/api/account/register")
        def account_register(payload: dict = Body(...)):
            username = payload.get("username")
            password = payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise HTTPException(status_code=400, detail="Username and password are required.")
            return register_account(username, password)

    if "/api/account/login" not in existing:
        @app.post("/api/account/login")
        def account_login(payload: dict = Body(...)):
            username = payload.get("username")
            password = payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise HTTPException(status_code=400, detail="Username and password are required.")
            return login_account(username, password)

    if "/api/account/session" not in existing:
        @app.post("/api/account/session")
        def account_session(payload: dict = Body(...)):
            token = payload.get("auth_token")
            if not isinstance(token, str):
                raise HTTPException(status_code=400, detail="auth_token is required.")
            account_id = verify_session(token)
            if account_id is None:
                raise HTTPException(status_code=401, detail="Session is invalid or expired.")
            return {"ok": True, "account_id": account_id}

    if "/api/account/history" not in existing:
        @app.post("/api/account/history")
        def account_bottle_history(payload: dict = Body(...)):
            token = payload.get("auth_token")
            if not isinstance(token, str):
                raise HTTPException(status_code=400, detail="auth_token is required.")
            account_id = verify_session(token)
            if account_id is None:
                raise HTTPException(status_code=401, detail="Session is invalid or expired.")
            return {
                "ok": True,
                "account_id": account_id,
                "history": account_history(account_id),
            }

    if "/api/account/purchase-status" not in existing:
        @app.post("/api/account/purchase-status")
        def account_purchase_status(payload: dict = Body(...)):
            token = payload.get("auth_token")
            if not isinstance(token, str):
                raise HTTPException(status_code=400, detail="auth_token is required.")
            account_id = verify_session(token)
            if account_id is None:
                raise HTTPException(status_code=401, detail="Session is invalid or expired.")
            return purchase_status(account_id)

    if "/api/account/apple-purchase" not in existing:
        @app.post("/api/account/apple-purchase")
        def account_apple_purchase(payload: dict = Body(...)):
            token = payload.get("auth_token")
            signed_transaction = payload.get("signed_transaction")
            if not isinstance(token, str) or not isinstance(signed_transaction, str):
                raise HTTPException(
                    status_code=400,
                    detail="auth_token and signed_transaction are required.",
                )
            return verify_and_grant_apple_purchase(token, signed_transaction)
