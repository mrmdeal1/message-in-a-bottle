"""Regression tests for the unattended bottle scheduler.

Runs against stubs only: no database, external ocean-current API, or production
bottle is accessed. Intentionally uses Python standard library only.
"""
import importlib.util
from datetime import datetime
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class MemoryCursor:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.commands = []

    def execute(self, query, params=None):
        self.commands.append((str(query), params))

    def fetchone(self):
        return (self.acquired,)


class MemoryConnection:
    def __init__(self, acquired=True):
        self.cursor_instance = MemoryCursor(acquired=acquired)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        class CursorContext:
            def __enter__(inner):
                return self.cursor_instance

            def __exit__(inner, *_):
                return False
        return CursorContext()


def load_scheduler():
    """Ensure imports cannot use live storage or network services."""
    storage = types.ModuleType("storage")
    storage.database_enabled = lambda: True
    storage._connect = lambda: MemoryConnection()
    storage.attach_account = lambda bottle, account, month: bottle.update({
        "_storage_account_id": account,
        "_storage_month_key": month,
    }) or bottle
    storage.save_bottle_if_unchanged = lambda new, old: True

    engine = types.ModuleType("bottle_engine")
    engine.move_bottle_live = lambda *args, **kwargs: None
    engine.update_bottle = lambda bottle, hours, persist: bottle

    sitecustomize = types.ModuleType("sitecustomize")
    sitecustomize._move_bottle_live_remote = engine.move_bottle_live

    with patch.dict(sys.modules, {
        "storage": storage,
        "bottle_engine": engine,
        "sitecustomize": sitecustomize,
    }):
        spec = importlib.util.spec_from_file_location(
            "bottle_daily_scheduler_under_test",
            Path(__file__).resolve().parents[1] / "bottle_daily_scheduler.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, storage, engine, sitecustomize


class BottleDailySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler, self.storage, self.engine, self.sitecustomize = load_scheduler()
        self.module_patch = patch.dict(sys.modules, {
            "storage": self.storage,
            "bottle_engine": self.engine,
            "sitecustomize": self.sitecustomize,
        })
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        from datetime import datetime, timezone
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

    def bottle(self, time="2026-10-22T12:00:00+00:00", status="drifting", opened=False):
        return {
            "status": status,
            "opened": opened,
            "current_time": time,
            "latitude": 25.0,
            "longitude": -85.0,
            "total_miles_traveled": 12.0,
            "journey_history": [{"event": "launched", "latitude": 25.0,
                                 "longitude": -85.0, "time": time}],
        }

    def test_exactly_24_hours_is_eligible(self):
        self.assertTrue(self.scheduler._eligible(self.bottle(), self.now))

    def test_23_hours_is_not_eligible(self):
        state = self.bottle("2026-10-22T13:00:00+00:00")
        self.assertFalse(self.scheduler._eligible(state, self.now))

    def test_completed_and_opened_bottles_never_advance(self):
        for status in ("ashore", "lost_at_sea", "journey_ended"):
            with self.subTest(status=status):
                self.assertFalse(self.scheduler._eligible(
                    self.bottle(status=status), self.now))
        self.assertFalse(self.scheduler._eligible(self.bottle(opened=True), self.now))

    def test_invalid_timestamp_is_safely_skipped(self):
        self.assertFalse(self.scheduler._eligible(self.bottle("not-a-time"), self.now))
        self.assertFalse(self.scheduler._eligible(self.bottle(None), self.now))

    def test_naive_utc_timestamp_is_supported(self):
        self.assertTrue(self.scheduler._eligible(
            self.bottle("2026-10-22T11:59:59"), self.now))

    def test_one_bottle_only_advances_in_four_six_hour_steps(self):
        original = self.bottle("2026-10-21T12:00:00+00:00")
        candidates = [("acct-one", "2026-10", "bottle-one", original)]
        moves = []
        saved = []
        self.scheduler._candidate_bottles = lambda conn: candidates
        self.scheduler.MAX_BOTTLES = 8
        self.scheduler.DRY_RUN = False

        def advance(bottle, hours, persist):
            from datetime import datetime, timedelta
            self.assertFalse(persist)
            moves.append(hours)
            bottle["current_time"] = (
                datetime.fromisoformat(bottle["current_time"])
                + timedelta(hours=hours)
            ).isoformat()
            bottle["total_miles_traveled"] += 2
            bottle.setdefault("route_points", []).append({
                "time": bottle["current_time"],
                "latitude": 25.0,
                "longitude": -85.0 + bottle["total_miles_traveled"] / 1000,
            })
            return bottle

        self.engine.update_bottle = advance
        self.storage.save_bottle_if_unchanged = lambda new, old: saved.append(
            (new.copy(), old.copy())
        ) or True
        with patch.object(self.scheduler, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.scheduler.run()

        self.assertEqual(moves, [6, 6, 6, 6])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0]["total_miles_traveled"], 20)
        self.assertEqual(len(saved[0][0]["route_points"]), 4)
        # In-memory changes must not mutate the original DB record.
        self.assertEqual(original["total_miles_traveled"], 12.0)

    def test_owner_change_prevents_overwrite(self):
        old = self.bottle("2026-10-21T12:00:00+00:00")
        self.scheduler._candidate_bottles = lambda conn: [
            ("acct-one", "2026-10", "bottle-one", old)
        ]
        self.scheduler.DRY_RUN = False
        self.engine.update_bottle = lambda bottle, hours, persist: bottle.update({
            "total_miles_traveled": bottle["total_miles_traveled"] + 1
        })
        self.storage.save_bottle_if_unchanged = lambda new, previous: False
        with patch.object(self.scheduler, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.scheduler.run()
        self.assertEqual(old["total_miles_traveled"], 12.0)

    def test_worker_failure_does_not_commit_partial_state(self):
        old = self.bottle("2026-10-21T12:00:00+00:00")
        self.scheduler._candidate_bottles = lambda conn: [
            ("acct-one", "2026-10", "bottle-one", old)
        ]
        self.scheduler.DRY_RUN = False
        calls = []
        def failing_step(bottle, hours, persist):
            bottle["total_miles_traveled"] += 1
            raise RuntimeError("simulated ocean worker outage")
        self.engine.update_bottle = failing_step
        self.storage.save_bottle_if_unchanged = lambda *args: calls.append(True)
        with patch.object(self.scheduler, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            with self.assertRaisesRegex(RuntimeError, "1 bottle errors"):
                self.scheduler.run()
        self.assertEqual(calls, [])
        self.assertEqual(old["total_miles_traveled"], 12.0)

    def test_dry_run_never_calculates_or_saves(self):
        self.scheduler._candidate_bottles = lambda conn: [
            ("acct-one", "2026-10", "bottle-one", self.bottle())
        ]
        self.scheduler.DRY_RUN = True
        self.engine.update_bottle = lambda *args, **kwargs: self.fail("moved on dry run")
        self.storage.save_bottle_if_unchanged = lambda *args: self.fail("saved on dry run")
        with patch.object(self.scheduler, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.scheduler.run()

    def test_advisory_lock_prevents_overlap(self):
        self.storage._connect = lambda: MemoryConnection(acquired=False)
        self.scheduler._candidate_bottles = lambda conn: self.fail("queried without lock")
        self.scheduler.run()


if __name__ == "__main__":
    unittest.main()
