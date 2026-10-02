#!/usr/bin/env python3
"""Split local INTERVAL CSV data by submission count; shared with the notebook."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Dict, Optional, Sequence

import pandas as pd

from project_paths import project_root


PLACE_COLUMNS = [
    "Id", "location_type", "tree_height", "geometry_x", "geometry_y",
    "dataset_name", "url",
]
OUTPUT_NAMES = ("places_with_single_submission.csv", "places_with_multiple_submissions.csv")


@dataclass
class ProcessingResult:
    single: pd.DataFrame
    multiple: pd.DataFrame
    summary: Dict[str, int]


def require_columns(frame: pd.DataFrame, required: Sequence[str], label: str) -> None:
    if not frame.columns.is_unique:
        raise ValueError(f"{label}: duplicate column names are not supported")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"{label}: missing required columns: {', '.join(missing)}")


def normalize_ids(values: pd.Series, label: str) -> pd.Series:
    """Accept positive integer IDs, including CSV representations such as 12.0."""
    text = values.astype("string").str.strip()
    valid = text.str.fullmatch(r"[0-9]+(?:\.0+)?").fillna(False)
    if not valid.all():
        raise ValueError(f"{label}: IDs must be nonblank positive integers")
    normalized = text.map(lambda value: str(int(value.split('.')[0])))
    if normalized.eq("0").any():
        raise ValueError(f"{label}: IDs must be positive integers")
    return normalized.astype("string")


def process_data(places: pd.DataFrame, submissions: pd.DataFrame) -> ProcessingResult:
    """Preserve the original inner join and Id_x/Id_y output column convention.

    A row represents a Submission, not a unique Place. Duplicate IDs are rejected
    instead of silently dropping records or inflating the submission counts.
    """
    require_columns(places, PLACE_COLUMNS, "Places")
    require_columns(submissions, ["Id", "location_id"], "Submissions")
    if "submission_count" in submissions.columns:
        raise ValueError("Submissions: 'submission_count' is reserved for the calculated count")
    left = places.loc[:, PLACE_COLUMNS].copy()
    right = submissions.copy()
    left["Id"] = normalize_ids(left["Id"], "Places.Id")
    right["Id"] = normalize_ids(right["Id"], "Submissions.Id")
    right["location_id"] = normalize_ids(right["location_id"], "Submissions.location_id")
    for frame, label in ((left, "Places"), (right, "Submissions")):
        if frame["Id"].duplicated().any():
            raise ValueError(f"{label}: duplicate Id values; correct the source CSV before processing")

    # Avoid ambiguous names when an input already contains pandas merge suffixes.
    overlap = set(left.columns) & set(right.columns)
    output_columns = [c + "_x" if c in overlap else c for c in left.columns]
    output_columns += [c + "_y" if c in overlap else c for c in right.columns]
    if len(output_columns) != len(set(output_columns)):
        raise ValueError("Input columns collide after applying merge suffixes _x and _y")

    merged = left.merge(right, left_on="Id", right_on="location_id", how="inner",
                        sort=False, validate="one_to_many")
    merged["submission_count"] = merged.groupby("Id_x")["Id_y"].transform("size").astype("int64")
    single = merged.loc[merged["submission_count"].eq(1)].copy()
    multiple = merged.loc[merged["submission_count"].gt(1)].copy()
    summary = {
        "input_places": len(left),
        "input_submissions": len(right),
        "matched_places": int(merged["Id_x"].nunique()),
        "matched_submissions": len(merged),
        "places_without_submissions": int((~left["Id"].isin(right["location_id"])).sum()),
        "unmatched_submissions": int((~right["location_id"].isin(left["Id"])).sum()),
        "single_submission_places": len(single),
        "multiple_submission_places": int(multiple["Id_x"].nunique()),
        "multiple_submission_rows": len(multiple),
    }
    return ProcessingResult(single, multiple, summary)


def read_csv(path: Path) -> pd.DataFrame:
    """Read source values as text so large IDs and literal 'NA' are preserved."""
    import csv

    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), [])
    if not header:
        raise ValueError(f"{path}: empty file; a CSV header is required")
    if len(header) != len(set(header)):
        raise ValueError(f"{path}: duplicate column names are not supported")
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def load_and_process(places_path: Path, submissions_path: Path) -> ProcessingResult:
    return process_data(read_csv(places_path), read_csv(submissions_path))


def write_outputs(result: ProcessingResult, output_dir: Path,
                  input_paths: Sequence[Path] = ()) -> Dict[str, Path]:
    output_dir = Path(output_dir)
    destinations = [output_dir / name for name in OUTPUT_NAMES]
    inputs = {Path(path).resolve() for path in input_paths}
    if any(path.resolve() in inputs for path in destinations):
        raise ValueError("An output path overlaps an input CSV; choose another output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    for frame, path in zip((result.single, result.multiple), destinations):
        frame.to_csv(path, index=False, encoding="utf-8")
    return dict(zip(("single", "multiple"), destinations))


def main(argv: Optional[Sequence[str]] = None) -> int:
    default_dir = project_root() / "outputs" / "csv_files"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--places", type=Path, default=default_dir / "places.csv")
    parser.add_argument("--submissions", type=Path, default=default_dir / "submissions.csv")
    parser.add_argument("--output-dir", type=Path, default=default_dir)
    args = parser.parse_args(argv)
    try:
        result = load_and_process(args.places, args.submissions)
        paths = write_outputs(result, args.output_dir, (args.places, args.submissions))
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.summary, indent=2))
    for group, path in paths.items():
        print(f"{group}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
