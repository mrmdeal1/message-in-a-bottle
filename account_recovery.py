"""Vicissitude Version 7 account recovery (development branch only).

Email must be VERIFIED while signed in before it can recover an account.
Required env: VICISSITUDE_SMTP_HOST, VICISSITUDE_SMTP_USERNAME,
VICISSITUDE_SMTP_PASSWORD, VICISSITUDE_FROM_EMAIL,
VICISSITUDE_RECOVERY_SECRET (a long random secret).
SMTP uses TLS: port 465 (implicit) or 587 (STARTTLS).
Never logs codes or exposes whether a recovery email exists.
"""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

from fastapi import Body, HTTPException
from psycopg.errors import UniqueViolation

import storage

CODE_LIFETIME_MINUTES = 15
MAX_ATTEMPTS = 5
MAX_SENDS_PER_HOUR = 3
EMAIL_PATTERN = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$")


def _email(value):
    email = str(value or "").strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    return email


def _configured():
    names = ("VICISSITUDE_SMTP_HOST", "VICISSITUDE_SMTP_USERNAME",
             "VICISSITUDE_SMTP_PASSWORD", "VICISSITUDE_FROM_EMAIL",
             "VICISSITUDE_RECOVERY_SECRET")
    return all(os.getenv(n) for n in names) and len(os.getenv("VICISSITUDE_RECOVERY_SECRET", "")) >= 32


def _require_email():
    if not _configured():
        raise HTTPException(status_code=503, detail="Email recovery is not available yet. Please contact Vicissitude support.")


def _code_hash(challenge_id, email, purpose, code):
    secret = os.environ["VICISSITUDE_RECOVERY_SECRET"].encode("utf-8")
    message = f"{challenge_id}|{email}|{purpose}|{code}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _send_email(address, subject, body):
    _require_email()
    msg = EmailMessage()
    msg["From"] = os.environ["VICISSITUDE_FROM_EMAIL"]
    msg["To"] = address
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ["VICISSITUDE_SMTP_HOST"]
    port = int(os.getenv("VICISSITUDE_SMTP_PORT", "587"))
    if port not in (465, 587):
        raise RuntimeError("Recovery SMTP must use TLS port 465 or 587.")
    context = ssl.create_default_context()
    if port == 465:
        client = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=15)
    with client as smtp:
        if port == 587:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(os.environ["VICISSITUDE_SMTP_USERNAME"], os.environ["VICISSITUDE_SMTP_PASSWORD"])
        smtp.send_message(msg)


def _init():
    if not storage.database_enabled():
        raise HTTPException(status_code=503, detail="Account recovery is temporarily unavailable.")
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS account_recovery_emails (
                  account_id TEXT PRIMARY KEY REFERENCES users(account_id) ON DELETE CASCADE,
                  email_normalized TEXT NOT NULL UNIQUE,
                  verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS account_recovery_codes (
                  challenge_id TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL REFERENCES users(account_id) ON DELETE CASCADE,
                  email_normalized TEXT NOT NULL,
                  purpose TEXT NOT NULL CHECK (purpose IN ('enroll', 'reset')),
                  code_hash TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '15 minutes'),
                  attempts INTEGER NOT NULL DEFAULT 0,
                  used_at TIMESTAMPTZ
                )""")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS recovery_codes_rate_idx
                ON account_recovery_codes (email_normalized, purpose, created_at DESC)
                """)


def _account(auth_token):
    import auth_support
    if not isinstance(auth_token, str):
        raise HTTPException(status_code=401, detail="Please sign in again.")
    account_id = auth_support.verify_session(auth_token)
    if not account_id:
        raise HTTPException(status_code=401, detail="Please sign in again.")
    return account_id


def _issue(account_id, address, purpose):
    _require_email()
    _init()
    challenge = secrets.token_urlsafe(24)
    code = str(secrets.randbelow(100_000_000)).zfill(8)
    digest = _code_hash(challenge, address, purpose, code)
    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MAX(created_at)
                FROM account_recovery_codes
                WHERE email_normalized=%s AND purpose=%s
                  AND created_at > NOW() - INTERVAL '1 hour'
            """, (address, purpose))
            count, newest = cur.fetchone()
            if count >= MAX_SENDS_PER_HOUR or (newest and (datetime.now(timezone.utc)-newest).total_seconds()<45):
                if purpose == "reset":
                    return False
                raise HTTPException(status_code=429, detail="Please wait before requesting another code.")
            cur.execute("""
                INSERT INTO account_recovery_codes
                  (challenge_id, account_id, email_normalized, purpose, code_hash)
                VALUES (%s,%s,%s,%s,%s)
            """, (challenge, account_id, address, purpose, digest))
    if purpose == "enroll":
        subject = "Vicissitude - confirm your recovery email"
        body = ("Confirm this address for Vicissitude account recovery.\n\n"
                f"Your one-time verification code: {code}\n"
                "Expires in 15 minutes. If you didn't request this, ignore the email.\n")
    else:
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT display_username FROM users WHERE account_id=%s", (account_id,))
                row = cur.fetchone()
        username = row[0] if row else ""
        subject = "Vicissitude - username and password recovery"
        body = (f"Your Vicissitude username: {username}\n\n"
                f"Password reset code: {code}\n"
                "Enter this code inside the Vicissitude app within 15 minutes.\n"
                "If you didn't request this, ignore this email. No changes were made.\n")
    try:
        _send_email(address, subject, body)
    except Exception:
        # Do not leave a usable challenge if mail could not be sent.
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE account_recovery_codes SET used_at=NOW() WHERE challenge_id=%s", (challenge,))
        if purpose == "enroll":
            raise HTTPException(status_code=503, detail="Could not send verification email. Please try again later.")
        return False
    return True


def _consume(cur, account_id, address, purpose, code):
    cur.execute("""
        SELECT challenge_id, code_hash, attempts, expires_at
        FROM account_recovery_codes
        WHERE account_id=%s AND email_normalized=%s AND purpose=%s
          AND used_at IS NULL
        ORDER BY created_at DESC LIMIT 1 FOR UPDATE
    """, (account_id, address, purpose))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    challenge, expected, attempts, expires_at = row
    if attempts >= MAX_ATTEMPTS or expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    if not isinstance(code, str) or not re.fullmatch(r"\d{8}", code) or not hmac.compare_digest(expected, _code_hash(challenge, address, purpose, code)):
        cur.execute("UPDATE account_recovery_codes SET attempts=attempts+1 WHERE challenge_id=%s", (challenge,))
        return None
    cur.execute("UPDATE account_recovery_codes SET used_at=NOW() WHERE challenge_id=%s", (challenge,))
    return True


def _masked(address):
    local, domain = address.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain


def install_recovery_routes(app):
    existing = {getattr(r, "path", None) for r in getattr(app, "routes", [])}

    if "/api/account/recovery-email/status" not in existing:
        @app.post("/api/account/recovery-email/status")
        def recovery_email_status(payload: dict = Body(...)):
            aid = _account(payload.get("auth_token"))
            _init()
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email_normalized FROM account_recovery_emails WHERE account_id=%s", (aid,))
                    row = cur.fetchone()
            return {"ok": True, "verified": bool(row), "masked_email": _masked(row[0]) if row else ""}

    if "/api/account/recovery-email/request" not in existing:
        @app.post("/api/account/recovery-email/request")
        def recovery_email_request(payload: dict = Body(...)):
            aid = _account(payload.get("auth_token"))
            address = _email(payload.get("email"))
            _init()
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT account_id FROM account_recovery_emails WHERE email_normalized=%s", (address,))
                    taken = cur.fetchone()
            if taken and taken[0] != aid:
                raise HTTPException(status_code=409, detail="This email is already linked to another account.")
            _issue(aid, address, "enroll")
            return {"ok": True, "message": "Verification code sent. Check your email."}

    if "/api/account/recovery-email/verify" not in existing:
        @app.post("/api/account/recovery-email/verify")
        def recovery_email_verify(payload: dict = Body(...)):
            aid = _account(payload.get("auth_token"))
            address = _email(payload.get("email"))
            _init()
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    valid = _consume(cur, aid, address, "enroll", payload.get("code"))
                    if not valid:
                        return {"ok": False, "message": "Invalid or expired code."}
                    try:
                        cur.execute("""
                            INSERT INTO account_recovery_emails (account_id, email_normalized)
                            VALUES (%s,%s)
                            ON CONFLICT (account_id)
                            DO UPDATE SET email_normalized=EXCLUDED.email_normalized, verified_at=NOW()
                        """, (aid, address))
                    except UniqueViolation:
                        raise HTTPException(status_code=409, detail="This email is already linked to another account.")
            return {"ok": True, "message": "Recovery email verified."}

    if "/api/account/recovery/start" not in existing:
        @app.post("/api/account/recovery/start")
        def recovery_start(payload: dict = Body(...)):
            _require_email()
            address = _email(payload.get("email"))
            _init()
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT account_id FROM account_recovery_emails WHERE email_normalized=%s", (address,))
                    row = cur.fetchone()
            if row:
                _issue(row[0], address, "reset")
            return {"ok": True, "message": "If this is a verified recovery email, a message has been sent with your username and a reset code."}

    if "/api/account/recovery/complete" not in existing:
        @app.post("/api/account/recovery/complete")
        def recovery_complete(payload: dict = Body(...)):
            import auth_support
            _require_email()
            address = _email(payload.get("email"))
            new_password = payload.get("new_password")
            if not isinstance(new_password, str) or not (8 <= len(new_password) <= 128):
                raise HTTPException(status_code=400, detail="Password must be between 8 and 128 characters.")
            _init()
            with storage._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT account_id FROM account_recovery_emails WHERE email_normalized=%s", (address,))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=400, detail="Invalid or expired code.")
                    aid = row[0]
                    valid = _consume(cur, aid, address, "reset", payload.get("code"))
                    if not valid:
                        return {"ok": False, "message": "Invalid or expired code."}
                    cur.execute("UPDATE users SET password_hash=%s WHERE account_id=%s",
                                (auth_support._hash_password(new_password), aid))
                    cur.execute("DELETE FROM account_sessions WHERE account_id=%s", (aid,))
                    cur.execute("UPDATE account_recovery_codes SET used_at=NOW() WHERE account_id=%s AND purpose='reset'", (aid,))
            return {"ok": True, "message": "Password updated. Sign in with your username and new password."}
