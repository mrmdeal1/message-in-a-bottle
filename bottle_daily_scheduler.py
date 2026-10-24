"""Vicissitude: advance unattended drifting bottles in bounded daily steps.

Intended for a paid Render Cron Job (e.g. every six hours).
This module is deliberately not connected to the current production deployment.

Every eligible bottle is advanced a maximum of 24 simulated hours per run,
using the existing six-hour current engine. Each step records a route checkpoint
via bottle_engine.update_bottle. The original record is saved only when it has
not changed meanwhile (protecting purchases, opening, replies, etc.).

Set BOTTLE_CRON_DRY_RUN=1 to inspect eligibility without modifying records.
"""
import copy
import logging
import os
from datetime import datetime, timezone

import storage

LOGGER = logging.getLogger("vicissitude.bottle_scheduler")
ADVISORY_LOCK = 74101826  # One active scheduler per database.
MAX_BOTTLES = max(1, min(100, int(os.getenv("BOTTLE_CRON_MAX_BOTTLES", "8"))))
MIN_OVERDUE_HOURS = 24
UPDATE_HOURS = 24
STEP_HOURS = 6
DRY_RUN = os.getenv("BOTTLE_CRON_DRY_RUN", "0") == "1"


def _utc(dt_str):
    if not isinstance(dt_str, str) or not dt_str.strip():
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _eligible(state, now):
    if not isinstance(state, dict):
        return False
    if state.get("status") != "drifting" or state.get("opened"):
        return False
    saved_time = _utc(state.get("current_time"))
    if saved_time is None:
        return False
    return (now - saved_time).total_seconds() >= MIN_OVERDUE_HOURS * 3600


def _candidate_bottles(conn):
    # Scan oldest records first. Recently advanced bottles move to the back,
    # providing fairness without loading the entire table into memory.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, month_key, bottle_id, state
            FROM bottles
            WHERE state->>'status' = 'drifting'
            ORDER BY updated_at ASC
            LIMIT 1000
            """
        )
        return cur.fetchall()


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not storage.database_enabled():
        raise RuntimeError("DATABASE_URL is required for scheduled movement")

    # The Python startup hook installs the existing remote ocean-current
    # movement function; never fall back to a different engine silently.
    import bottle_engine as engine
    import sitecustomize
    if engine.move_bottle_live is not sitecustomize._move_bottle_live_remote:
        raise RuntimeError("Live ocean-current worker patch is not installed")

    scanned = advanced = skipped = errors = 0
    now = datetime.now(timezone.utc)

    with storage._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,))
            if not cur.fetchone()[0]:
                LOGGER.info("Another scheduler already holds the lock; exiting")
                return
        try:
            for account_id, month_key, bottle_id, state in _candidate_bottles(conn):
                scanned += 1
                original = dict(state or {})
                if not _eligible(original, now):
                    skipped += 1
                    continue

                if advanced >= MAX_BOTTLES:
                    break

                if DRY_RUN:
                    LOGGER.info("DRY RUN eligible bottle %s (month %s)", bottle_id, month_key)
                    advanced += 1
                    continue

                storage.attach_account(original, account_id, month_key)
                updated = copy.deepcopy(original)
                try:
                    for _ in range(UPDATE_HOURS // STEP_HOURS):
                        if updated.get("status") != "drifting" or updated.get("opened"):
                            break
                        engine.update_bottle(updated, hours=STEP_HOURS, persist=False)

                    if updated != original:
                        if storage.save_bottle_if_unchanged(updated, original):
                            LOGGER.info("Advanced bottle %s for up to 24 h", bottle_id)
                            advanced += 1
                        else:
                            LOGGER.info("Bottle %s changed concurrently; deferred", bottle_id)
                            skipped += 1
                    else:
                        skipped += 1
                except Exception:
                    errors += 1
                    LOGGER.exception("Bottle %s could not advance; keeping saved record", bottle_id)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
    LOGGER.info("Complete: scanned=%s advanced=%s skipped=%s errors=%s dry_run=%s",
                scanned, advanced, skipped, errors, DRY_RUN)
    if errors:
        raise RuntimeError(f"Scheduled movement had {errors} bottle errors")


if __name__ == "__main__":
    run()
