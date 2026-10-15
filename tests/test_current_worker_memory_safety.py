import unittest
from unittest.mock import patch

from fastapi import HTTPException
import current_worker_server as worker


class CurrentWorkerMemorySafetyTests(unittest.TestCase):
    def test_second_heavy_job_is_rejected(self):
        self.assertTrue(worker._heavy_job_slot.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPException) as caught:
                worker._run_single_heavy_job(["ignored"])
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.headers["Retry-After"], "15")
        finally:
            worker._heavy_job_slot.release()

    def test_slot_released_when_subprocess_fails(self):
        with patch.object(worker.subprocess, "run", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                worker._run_single_heavy_job(["ignored"])
        self.assertTrue(worker._heavy_job_slot.acquire(blocking=False))
        worker._heavy_job_slot.release()


if __name__ == "__main__":
    unittest.main()
