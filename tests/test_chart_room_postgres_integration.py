"""Isolated integration: real V7 movement engine + real PostgreSQL save/load.
Run only against the disposable PostgreSQL instance created by GitHub Actions.
No production DATABASE_URL or Render service used.
"""
import os
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# -I isolates Python startup, avoiding the production sitecustomize hook.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Data-heavy modules are replaced before loading the REAL movement engine.
# The actual movement/state/route checkpoint code remains unmodified.
sys.modules["copernicusmarine"] = types.ModuleType("copernicusmarine")
sys.modules["rasterio"] = types.ModuleType("rasterio")
sys.modules["storm_detector"] = types.ModuleType("storm_detector")
sys.modules["storm_detector"].storm_level_for_bottle = lambda *a, **kw: ("calm", None)
sys.modules["build4_support"] = types.ModuleType("build4_support")

import storage
import bottle_engine as engine

# The scheduler verifies the remote worker patch is present. Provide a
# deterministic stand-in for its current-vector provider.
def sample_current(lat, lon, date, hours=6):
    return lat + 0.02, lon + 0.03, 1.5

worker = types.ModuleType("sitecustomize")
worker._move_bottle_live_remote = sample_current
sys.modules["sitecustomize"] = worker
engine.move_bottle_live = sample_current

# These are ocean geometry/storm conditions only. All bottle movement,
# event creation, waypoint capture and PostgreSQL saves are REAL production code.
engine.maybe_destroy_bottle = lambda bottle, **kwargs: bottle
engine.maybe_encounter_real_coast = lambda bottle, **kwargs: bottle
engine.movement_path_coast_event = lambda *args: (999.0, None, None, None)
engine.update_coast_arm_state = lambda *args: None
engine.broad_region = lambda *args: "Gulf of Mexico"

import bottle_daily_scheduler as scheduler


class RealDatabaseMovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fail closed: do not run unless bound to the test-only GH Actions DB.
        address = os.environ.get("DATABASE_URL", "")
        assert "127.0.0.1:5432/vicissitude_test" in address, (
            "Refusing to test: DATABASE_URL is not the disposable localhost test database"
        )
        storage.init_db()

    def setUp(self):
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE bottles, entitlements")
        scheduler.DRY_RUN = False
        scheduler.MAX_BOTTLES = 8
        engine.move_bottle_live = sample_current

    def tearDown(self):
        with storage._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE bottles, entitlements")

    def add_bottle(self, account, age_hours, status="drifting"):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        started = (now - timedelta(hours=age_hours)).isoformat()
        bottle = engine.create_bottle(25.0, -85.0, start_time=started,
                                      sender_id=account, message="A test message")
        bottle["status"] = status
        month = datetime.fromisoformat(started).strftime("%Y-%m")
        storage.attach_account(bottle, account, month)
        storage.save_bottle(bottle)
        return account, month, bottle

    def fetch(self, account, month):
        result = storage.load_bottle(account, month)
        self.assertIsNotNone(result)
        return result

    def test_overdue_bottle_travels_and_persists_five_route_points(self):
        account, month, original = self.add_bottle("test-overdue", 48)
        newer_account, newer_month, newer = self.add_bottle("test-recent", 3)
        scheduler.run()

        saved = self.fetch(account, month)
        self.assertEqual(saved["bottle_id"], original["bottle_id"])
        self.assertEqual(saved["status"], "drifting")
        self.assertEqual(len(saved["route_points"]), 5)  # launch + four six-hour moves
        self.assertEqual(saved["route_points"][0]["latitude"], 25.0)
        self.assertAlmostEqual(saved["route_points"][-1]["latitude"], 25.08)
        self.assertAlmostEqual(saved["route_points"][-1]["longitude"], -84.88)
        self.assertAlmostEqual(saved["total_miles_traveled"], 6.0)
        self.assertEqual(
            datetime.fromisoformat(saved["current_time"]) -
            datetime.fromisoformat(original["current_time"]),
            timedelta(hours=24),
        )
        for position in saved["route_points"]:
            self.assertIn("latitude", position)
            self.assertIn("longitude", position)
            self.assertIn("time", position)

        recent_saved = self.fetch(newer_account, newer_month)
        self.assertEqual(recent_saved["total_miles_traveled"], 0.0)
        self.assertFalse(recent_saved.get("route_points"))
        print("PASS: independent overdue journey, 5 real checkpoints persisted; recent bottle unchanged")

    def test_new_owner_activity_wins_atomic_save_conflict(self):
        account, month, original = self.add_bottle("test-race", 49)
        first_step = engine.update_bottle
        invoked = [False]

        def concurrent_update(bottle, *args, **kwargs):
            moved = first_step(bottle, *args, **kwargs)
            if not invoked[0]:
                invoked[0] = True
                active = self.fetch(account, month)
                active["message"] = "The owner changed this while calculation ran"
                storage.save_bottle(active)
            return moved

        engine.update_bottle = concurrent_update
        try:
            scheduler.run()
        finally:
            engine.update_bottle = first_step
        saved = self.fetch(account, month)
        self.assertTrue(invoked[0])
        self.assertEqual(saved["message"], "The owner changed this while calculation ran")
        self.assertAlmostEqual(saved["total_miles_traveled"], 0.0)
        self.assertFalse(saved.get("route_points"))
        print("PASS: concurrent account action protected by real PostgreSQL compare-and-save")

    def test_failed_current_lookup_leaves_persisted_state_unchanged(self):
        account, month, original = self.add_bottle("test-failure", 49)
        attempts = [0]

        def flaky_current(lat, lon, date, hours=6):
            attempts[0] += 1
            if attempts[0] == 2:
                raise RuntimeError("Test-only simulated currents outage")
            return sample_current(lat, lon, date, hours)

        engine.move_bottle_live = flaky_current
        with self.assertRaisesRegex(RuntimeError, "1 bottle errors"):
            scheduler.run()
        saved = self.fetch(account, month)
        self.assertEqual(saved["current_time"], original["current_time"])
        self.assertEqual(saved["total_miles_traveled"], 0.0)
        self.assertFalse(saved.get("route_points"))
        print("PASS: failure after one computed step preserves original persisted bottle")

    def test_completed_bottle_does_not_move(self):
        account, month, original = self.add_bottle("test-ashore", 72, status="ashore")
        scheduler.run()
        saved = self.fetch(account, month)
        self.assertEqual(saved["status"], "ashore")
        self.assertEqual(saved["total_miles_traveled"], 0.0)
        print("PASS: ashore bottle preserved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
