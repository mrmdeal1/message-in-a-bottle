"""Regression tests for finite Copernicus currents and isolated worker failures.

All datasets and child processes are mocked; no requests reach Render or
Copernicus, and no customer bottle data is touched.
"""
import contextlib
import importlib
import io
import json
import math
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr
from fastapi import HTTPException


# Avoid loading Copernicus' online client during unit tests.
sys.modules.setdefault("copernicusmarine", types.ModuleType("copernicusmarine"))
lookup = importlib.import_module("copernicus_lookup")
worker = importlib.import_module("current_worker_server")


def sample_grid(u, v, latitudes=None, longitudes=None):
    if latitudes is None:
        latitudes = [25.0, 25.0833]
    if longitudes is None:
        longitudes = [-85.0, -84.9167]
    shape = (1, 1, len(latitudes), len(longitudes))
    return xr.Dataset(
        data_vars={
            "uo": (("time", "depth", "latitude", "longitude"), np.asarray(u).reshape(shape)),
            "vo": (("time", "depth", "latitude", "longitude"), np.asarray(v).reshape(shape)),
        },
        coords={
            "time": [np.datetime64("2026-11-12")],
            "depth": [lookup.SURFACE_DEPTH],
            "latitude": latitudes,
            "longitude": longitudes,
        },
    )


class CopernicusValidityTests(unittest.TestCase):
    def test_nearest_masked_cell_uses_nearby_measured_cell(self):
        ds = sample_grid(
            [[float("nan"), 0.25], [0.19, 0.21]],
            [[float("nan"), -0.07], [-0.02, 0.03]],
        )
        self.assertEqual(
            lookup.nearest_valid_surface_current(ds, 25.0, -85.0),
            (0.25, -0.07),
        )

    def test_all_invalid_values_are_rejected(self):
        ds = sample_grid(
            [[float("nan"), float("nan")], [float("inf"), float("nan")]],
            [[float("nan"), float("nan")], [0.02, float("nan")]],
        )
        with self.assertRaisesRegex(ValueError, "No usable finite ocean current"):
            lookup.nearest_valid_surface_current(ds, 25.0, -85.0)

    def test_far_away_valid_cell_is_not_used(self):
        ds = sample_grid([[float("nan"), 0.3]], [[float("nan"), 0.2]],
                         latitudes=[25.0], longitudes=[-85.0, -84.0])
        with self.assertRaisesRegex(ValueError, "No usable finite ocean current"):
            lookup.nearest_valid_surface_current(ds, 25.0, -85.0)

    def test_subprocess_only_prints_finite_coordinates(self):
        ds = sample_grid([[float("nan"), 0.25], [0.19, 0.21]],
                         [[float("nan"), -0.07], [-0.02, 0.03]])
        output = io.StringIO()
        with patch.object(lookup.copernicusmarine, "open_dataset", return_value=ds), \
                patch.object(sys, "argv", ["copernicus_lookup.py", "25", "-85", "2026-11-12", "1"]), \
                contextlib.redirect_stdout(output):
            lookup.main()
        result = json.loads(output.getvalue(), parse_constant=lambda val: self.fail(val))
        self.assertTrue(all(math.isfinite(value) for value in result.values()))


class RenderWorkerValidityTests(unittest.TestCase):
    def test_rejects_nan_child_without_serialization_crash(self):
        response = subprocess.CompletedProcess([], 0,
            stdout='{"latitude": NaN, "longitude": -85}', stderr="")
        with patch.object(worker, "_run_single_heavy_job", return_value=response):
            with self.assertRaises(HTTPException) as raised:
                worker.current(25.0, -85.0, "2026-11-12")
        self.assertEqual(raised.exception.status_code, 502)

    def test_missing_valid_ocean_cell_is_422(self):
        problem = subprocess.CalledProcessError(
            1, ["python", "copernicus_lookup.py"],
            stderr="ValueError: No usable finite ocean current within 30 km")
        with patch.object(worker, "_run_single_heavy_job", side_effect=problem):
            with self.assertRaises(HTTPException) as raised:
                worker.current(25.0, -85.0, "2026-11-12")
        self.assertEqual(raised.exception.status_code, 422)

    def test_valid_child_returns_finite_velocity(self):
        response = subprocess.CompletedProcess([], 0,
            stdout='{"latitude": 25.001, "longitude": -85.002}', stderr="")
        with patch.object(worker, "_run_single_heavy_job", return_value=response):
            velocity = worker.current(25.0, -85.0, "2026-11-12")
        self.assertTrue(math.isfinite(velocity["u"]))
        self.assertTrue(math.isfinite(velocity["v"]))


if __name__ == "__main__":
    unittest.main()
