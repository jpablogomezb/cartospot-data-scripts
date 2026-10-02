"""Offline regression tests using synthetic data only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from INTERVAL_process_data import load_and_process, process_data, read_csv, write_outputs

FIXTURES = ROOT / "tests" / "fixtures" / "interval_local"


class LocalIntervalTests(unittest.TestCase):
    def setUp(self):
        self.places = read_csv(FIXTURES / "places.csv")
        self.submissions = read_csv(FIXTURES / "submissions.csv")

    def test_split_and_exclusions(self):
        result = process_data(self.places, self.submissions)
        self.assertEqual(result.single.Id_x.tolist(), ["1"])
        self.assertEqual(result.multiple.Id_y.tolist(), ["11", "12"])
        self.assertEqual(result.multiple.submission_count.tolist(), [2, 2])
        self.assertEqual(result.summary, {
            "input_places": 3, "input_submissions": 4, "matched_places": 2,
            "matched_submissions": 3, "places_without_submissions": 1,
            "unmatched_submissions": 1, "single_submission_places": 1,
            "multiple_submission_places": 1, "multiple_submission_rows": 2,
        })
        self.assertEqual(result.multiple.notes.tolist(), ["NA", "Árbol revisado"])
        self.assertEqual(result.multiple.species.tolist(), ["Birch", "Other"])
        self.assertIn("url_x", result.single)
        self.assertIn("url_y", result.single)

    def test_legacy_output_parity_on_valid_input(self):
        legacy = self.places.merge(self.submissions, left_on="Id", right_on="location_id", how="inner")
        counts = legacy.groupby("Id_x").size().reset_index(name="submission_count")
        legacy = legacy.merge(counts, on="Id_x")
        result = process_data(self.places, self.submissions)
        pd.testing.assert_frame_equal(result.single.reset_index(drop=True),
            legacy[legacy.submission_count.eq(1)].reset_index(drop=True), check_dtype=False)
        pd.testing.assert_frame_equal(result.multiple.reset_index(drop=True),
            legacy[legacy.submission_count.gt(1)].reset_index(drop=True), check_dtype=False)

    def test_duplicate_ids_rejected(self):
        for kind in ("places", "submissions"):
            with self.subTest(kind=kind):
                p, s = self.places, self.submissions
                if kind == "places": p = pd.concat([p, p.iloc[[0]]])
                else: s = pd.concat([s, s.iloc[[0]]])
                with self.assertRaisesRegex(ValueError, "duplicate Id"):
                    process_data(p, s)

    def test_missing_columns(self):
        for name in ("Id", "location_id"):
            with self.subTest(column=name), self.assertRaisesRegex(ValueError, "missing required columns"):
                process_data(self.places, self.submissions.drop(columns=name))
        with self.assertRaisesRegex(ValueError, "geometry_x"):
            process_data(self.places.drop(columns="geometry_x"), self.submissions)

    def test_invalid_ids(self):
        for value in ("", "abc", "1.5", "0", "-2", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integers"):
                process_data(self.places, self.submissions.assign(location_id=value))

    def test_id_normalization_and_large_ids(self):
        p = self.places.iloc[[0]].copy()
        s = self.submissions.iloc[[0]].copy()
        p["Id"] = " 0009007199254740993 "
        s["location_id"] = "9007199254740993.0"
        self.assertEqual(process_data(p, s).single.Id_x.tolist(), ["9007199254740993"])

    def test_empty_and_unmatched_inputs(self):
        cases = [(self.places.iloc[:0], self.submissions),
                 (self.places, self.submissions.iloc[:0]),
                 (self.places, self.submissions.assign(location_id="99"))]
        for p, s in cases:
            result = process_data(p, s)
            with tempfile.TemporaryDirectory() as tmp:
                for path in write_outputs(result, Path(tmp)).values():
                    exported = pd.read_csv(path)
                    self.assertTrue(exported.empty)
                    self.assertIn("Id_x", exported.columns)
                    self.assertIn("submission_count", exported.columns)

    def test_column_collisions(self):
        for name in ("submission_count", "Id_x", "url_x"):
            with self.subTest(column=name), self.assertRaises(ValueError):
                process_data(self.places, self.submissions.assign(**{name: "x"}))

    def test_read_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            with self.assertRaises(FileNotFoundError): read_csv(path)
            path.write_text("")
            with self.assertRaisesRegex(ValueError, "empty file"): read_csv(path)
            path.write_text("Id,Id\n1,2\n")
            with self.assertRaisesRegex(ValueError, "duplicate column"): read_csv(path)

    def test_input_overwrite_rejected(self):
        result = process_data(self.places, self.submissions)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "places_with_single_submission.csv"
            path.write_text("keep me")
            with self.assertRaisesRegex(ValueError, "overlaps"):
                write_outputs(result, Path(tmp), [path])
            self.assertEqual(path.read_text(), "keep me")

    def test_cli_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, str(ROOT / "src" / "INTERVAL_process_data.py"),
                       "--places", str(FIXTURES / "places.csv"),
                       "--submissions", str(FIXTURES / "submissions.csv"), "--output-dir", tmp]
            done = subprocess.run(command, cwd=tmp, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(pd.read_csv(Path(tmp) / "places_with_multiple_submissions.csv")), 2)
            command[command.index("--places") + 1] = str(Path(tmp) / "missing.csv")
            failed = subprocess.run(command, cwd=tmp, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("Error:", failed.stderr)
            self.assertNotIn("Traceback", failed.stderr)

    def test_notebook_cells_match_script(self):
        # Execute every Python cell in order with synthetic input paths. This
        # checks notebook code, not the Jupyter front end or kernel integration.
        notebook = json.loads((ROOT / "src" / "INTERVAL_process_data.ipynb").read_text())
        original_cwd = Path.cwd()
        original_sys_path = sys.path.copy()
        expected = load_and_process(FIXTURES / "places.csv", FIXTURES / "submissions.csv")
        try:
            for cwd in (ROOT, ROOT / "src"):
                with self.subTest(cwd=cwd), tempfile.TemporaryDirectory() as tmp:
                    os.chdir(cwd)
                    scope = {}
                    with contextlib.redirect_stdout(io.StringIO()):
                        for cell in notebook["cells"]:
                            if cell["cell_type"] != "code": continue
                            code = "".join(cell["source"])
                            exec(compile(code, "INTERVAL_process_data.ipynb", "exec"), scope)
                            if "PLACES_PATH =" in code:
                                scope.update(PLACES_PATH=FIXTURES / "places.csv",
                                             SUBMISSIONS_PATH=FIXTURES / "submissions.csv", OUTPUT_DIR=Path(tmp))
                    pd.testing.assert_frame_equal(scope["result"].single, expected.single)
                    pd.testing.assert_frame_equal(scope["result"].multiple, expected.multiple)
                    for name, frame in (("single", expected.single), ("multiple", expected.multiple)):
                        self.assertEqual(scope["output_paths"][name].read_text(), frame.to_csv(index=False))
        finally:
            os.chdir(original_cwd)
            sys.path[:] = original_sys_path


if __name__ == "__main__":
    unittest.main()
