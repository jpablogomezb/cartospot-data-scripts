#!/usr/bin/env python3
"""
CartoSpot dataset export utility.

Purpose
-------
Export a CartoSpot dataset into:
1. One dataset-level Places summary file (CSV + XLSX)
2. One per-Place submissions file (CSV + XLSX) for a selected submission set
3. Downloaded submission attachments (currently intended mainly for images)

Typical use case
----------------
Datasets with a small number of Places and many submission entries under a
specific submission set such as: contributions, inventory, comments, reviews,
etc.

Example
-------
python cartospot_dataset_export.py \
  --dataset-url https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal \
  --submission-set contributions \
  --output-dir ../outputs/exports

Notes
-----
- The script writes CSV and XLSX. XLSX is used instead of legacy XLS because it
  is more reliable with current Python tooling and supports larger sheets.
- Dynamic Place and Submission fields are exported automatically.
- Attachment URLs are preserved in the spreadsheets and downloaded to disk.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests

from project_paths import outputs_dir

TIMEOUT = 60
USER_AGENT = "CartoSpot Dataset Export/1.0"

PLACE_RESERVED_KEYS = {
    "id",
    "dataset",
    "attachments",
    "submitter",
    "visible",
    "created_datetime",
    "updated_datetime",
    "url",
    "user_token",
    "location_type",
    "location_default",
    "location",  # legacy compatibility
    "submission_sets",
}

SUBMISSION_RESERVED_KEYS = {
    "url",
    "id",
    "attachments",
    "submitter",
    "dataset",
    "set",
    "place",
    "created_datetime",
    "updated_datetime",
    "visible",
    "user_token",
}

NOISY_RENDER_KEYS = {
    "csrfmiddlewaretoken",
}


@dataclass
class DatasetInfo:
    id: int
    slug: str
    display_name: str
    url: str
    places_url: str


class CartoSpotExporter:
    def __init__(
        self,
        dataset_url: str,
        submission_set_name: str,
        output_dir: Path,
        api_token: Optional[str] = None,
        delay_seconds: float = 0.0,
        skip_attachment_download: bool = False,
    ) -> None:
        self.dataset_url = dataset_url.rstrip("/")
        self.submission_set_name = submission_set_name.strip()
        self.output_dir = output_dir
        self.delay_seconds = delay_seconds
        self.skip_attachment_download = skip_attachment_download

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if api_token:
            self.session.headers.update({"Authorization": f"Token {api_token}"})

    def get_json(self, url: str) -> Dict[str, Any]:
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        return response.json()

    def get_paginated_results(self, url: str, results_key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        next_url = url
        while next_url:
            payload = self.get_json(next_url)
            page_items = payload.get(results_key, [])
            if not isinstance(page_items, list):
                raise ValueError(f"Expected '{results_key}' to be a list at {next_url}")
            items.extend(page_items)
            next_url = payload.get("metadata", {}).get("next")
        return items

    def fetch_dataset_info(self) -> DatasetInfo:
        payload = self.get_json(self.dataset_url)
        places_url = payload.get("places", {}).get("url")
        if not places_url:
            raise ValueError("Dataset response does not include places.url")
        return DatasetInfo(
            id=payload["id"],
            slug=payload["slug"],
            display_name=payload.get("display_name", payload["slug"]),
            url=payload["url"],
            places_url=places_url,
        )

    def fetch_places(self, places_url: str) -> List[Dict[str, Any]]:
        places: List[Dict[str, Any]] = []
        next_url = places_url
        while next_url:
            payload = self.get_json(next_url)
            features = payload.get("features", [])
            if not isinstance(features, list):
                raise ValueError(f"Expected 'features' to be a list at {next_url}")
            places.extend(features)
            next_url = payload.get("metadata", {}).get("next")
        return places

    def export(self) -> None:
        dataset = self.fetch_dataset_info()
        dataset_base_dir = self.output_dir / safe_filename(f"{dataset.slug}_{dataset.id}")
        dataset_base_dir.mkdir(parents=True, exist_ok=True)

        places = self.fetch_places(dataset.places_url)
        print(f"Fetched {len(places)} place(s) from dataset '{dataset.slug}'")

        place_summary_rows: List[Dict[str, Any]] = []

        for feature in places:
            row, place_folder = self.process_place(dataset, feature, dataset_base_dir)
            place_summary_rows.append(row)

        summary_base = dataset_base_dir / safe_filename(f"{dataset.slug}_{dataset.id}_places_summary")
        self.write_tabular_exports(place_summary_rows, summary_base)
        print(f"Wrote dataset summary files: {summary_base}.csv and {summary_base}.xlsx")

    def process_place(
        self,
        dataset: DatasetInfo,
        feature: Dict[str, Any],
        dataset_base_dir: Path,
    ) -> Tuple[Dict[str, Any], Path]:
        properties = feature.get("properties", {}) or {}
        place_id = properties.get("id") or feature.get("id")
        if place_id is None:
            raise ValueError("Place is missing an id")

        place_name = (
            properties.get("name")
            or properties.get("location_default")
            or properties.get("location")
            or f"place_{place_id}"
        )
        safe_place_name = safe_filename(str(place_name), max_len=80)
        place_folder = dataset_base_dir / safe_filename(
            f"{dataset.slug}_{dataset.id}_place_{place_id}_{safe_place_name}"
        )
        place_folder.mkdir(parents=True, exist_ok=True)

        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or [None, None]
        lon = coordinates[0] if len(coordinates) > 0 else None
        lat = coordinates[1] if len(coordinates) > 1 else None

        submission_set_info = (properties.get("submission_sets") or {}).get(self.submission_set_name)
        submission_count = 0
        submissions: List[Dict[str, Any]] = []
        submissions_file_base: Optional[Path] = None

        if submission_set_info and submission_set_info.get("url"):
            submissions = self.get_paginated_results(submission_set_info["url"], "results")
            submission_count = len(submissions)
            submissions_file_base = place_folder / safe_filename(
                f"{dataset.slug}_{dataset.id}_place_{place_id}_{safe_place_name}_{self.submission_set_name}"
            )
            submission_rows = self.build_submission_rows(dataset, feature, submissions)
            self.write_tabular_exports(submission_rows, submissions_file_base)
            print(
                f"  Place {place_id}: wrote {len(submission_rows)} {self.submission_set_name} row(s) "
                f"to {submissions_file_base}.csv/.xlsx"
            )

            if not self.skip_attachment_download:
                self.download_submission_attachments(
                    dataset=dataset,
                    place_id=int(place_id),
                    place_name=safe_place_name,
                    place_folder=place_folder,
                    submissions=submissions,
                )
        else:
            print(
                f"  Place {place_id}: no '{self.submission_set_name}' submission set found; "
                f"only summary row will be exported"
            )

        summary_row = self.build_place_summary_row(
            dataset=dataset,
            feature=feature,
            submissions=submissions,
            place_id=int(place_id),
            place_name=str(place_name),
            lon=lon,
            lat=lat,
            submission_count=submission_count,
            submission_set_url=(submission_set_info or {}).get("url"),
            submissions_file_base=submissions_file_base,
        )
        return summary_row, place_folder

    def build_place_summary_row(
        self,
        dataset: DatasetInfo,
        feature: Dict[str, Any],
        submissions: List[Dict[str, Any]],
        place_id: int,
        place_name: str,
        lon: Any,
        lat: Any,
        submission_count: int,
        submission_set_url: Optional[str],
        submissions_file_base: Optional[Path],
    ) -> Dict[str, Any]:
        properties = feature.get("properties", {}) or {}
        place_attachments = properties.get("attachments") or []
        submitter = properties.get("submitter")
        submission_dates = [s.get("created_datetime") for s in submissions if s.get("created_datetime")]

        row: Dict[str, Any] = {
            "dataset_id": dataset.id,
            "dataset_slug": dataset.slug,
            "dataset_display_name": dataset.display_name,
            "dataset_url": dataset.url,
            "place_id": place_id,
            "place_name": place_name,
            "place_url": properties.get("url"),
            "longitude": lon,
            "latitude": lat,
            "location_type": properties.get("location_type"),
            "location_default": properties.get("location_default") or properties.get("location"),
            "visible": properties.get("visible"),
            "created_datetime": properties.get("created_datetime"),
            "updated_datetime": properties.get("updated_datetime"),
            "user_token": properties.get("user_token"),
            "submitter_id": submitter.get("id") if isinstance(submitter, dict) else None,
            "submitter_username": submitter.get("username") if isinstance(submitter, dict) else None,
            "submitter_name_display": (
                submitter.get("name") if isinstance(submitter, dict) else properties.get("submitter_name")
            ),
            "place_attachment_count": len(place_attachments),
            "place_attachment_urls": join_urls_from_attachments(place_attachments),
            "submission_set_name": self.submission_set_name,
            "submission_set_url": submission_set_url,
            "submission_count": submission_count,
            "first_submission_created_datetime": min(submission_dates) if submission_dates else None,
            "last_submission_created_datetime": max(submission_dates) if submission_dates else None,
            "submissions_csv_path": f"{submissions_file_base}.csv" if submissions_file_base else None,
            "submissions_xlsx_path": f"{submissions_file_base}.xlsx" if submissions_file_base else None,
        }

        for key, value in properties.items():
            if key in PLACE_RESERVED_KEYS:
                continue
            row[f"place_custom__{key}"] = normalize_for_cell(value)

        return row

    def build_submission_rows(
        self,
        dataset: DatasetInfo,
        feature: Dict[str, Any],
        submissions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        properties = feature.get("properties", {}) or {}
        place_id = properties.get("id") or feature.get("id")
        place_name = (
            properties.get("name")
            or properties.get("location_default")
            or properties.get("location")
            or f"place_{place_id}"
        )
        rows: List[Dict[str, Any]] = []

        for submission in submissions:
            submitter = submission.get("submitter")
            attachments = submission.get("attachments") or []
            row: Dict[str, Any] = {
                "dataset_id": dataset.id,
                "dataset_slug": dataset.slug,
                "dataset_display_name": dataset.display_name,
                "dataset_url": dataset.url,
                "submission_set_name": self.submission_set_name,
                "place_id": place_id,
                "place_name": place_name,
                "place_url": properties.get("url"),
                "submission_id": submission.get("id"),
                "submission_url": submission.get("url"),
                "submission_created_datetime": submission.get("created_datetime"),
                "submission_updated_datetime": submission.get("updated_datetime"),
                "submission_visible": submission.get("visible"),
                "submission_user_token": submission.get("user_token"),
                "submission_submitter_id": submitter.get("id") if isinstance(submitter, dict) else None,
                "submission_submitter_username": submitter.get("username") if isinstance(submitter, dict) else None,
                "submission_submitter_name_display": (
                    submitter.get("name") if isinstance(submitter, dict) else submission.get("submitter_name")
                ),
                "submission_attachment_count": len(attachments),
                "submission_attachment_urls": join_urls_from_attachments(attachments),
            }

            for idx, attachment in enumerate(attachments, start=1):
                row[f"attachment_{idx}_url"] = attachment.get("file")
                row[f"attachment_{idx}_name"] = attachment.get("name")
                row[f"attachment_{idx}_width"] = attachment.get("width")
                row[f"attachment_{idx}_height"] = attachment.get("height")

            for key, value in submission.items():
                if key in SUBMISSION_RESERVED_KEYS:
                    continue
                row[f"submission_custom__{key}"] = normalize_for_cell(value)

            rows.append(row)

        return rows

    def download_submission_attachments(
        self,
        dataset: DatasetInfo,
        place_id: int,
        place_name: str,
        place_folder: Path,
        submissions: List[Dict[str, Any]],
    ) -> None:
        attachments_dir = place_folder / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)

        for submission in submissions:
            submission_id = submission.get("id")
            attachments = submission.get("attachments") or []
            for idx, attachment in enumerate(attachments, start=1):
                file_url = attachment.get("file")
                if not file_url:
                    continue
                ext = infer_extension(file_url, attachment)
                file_name = safe_filename(
                    f"{dataset.slug}_{dataset.id}_place_{place_id}_{self.submission_set_name}_submission_{submission_id}_attachment_{idx}{ext}"
                )
                file_path = attachments_dir / file_name
                if file_path.exists():
                    continue
                self.download_file(file_url, file_path)
                print(f"    Downloaded attachment -> {file_path.name}")

    def download_file(self, url: str, path: Path) -> None:
        with self.session.get(url, stream=True, timeout=TIMEOUT) as response:
            response.raise_for_status()
            with open(path, "wb") as out_file:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        out_file.write(chunk)
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    @staticmethod
    def write_tabular_exports(rows: List[Dict[str, Any]], base_path: Path) -> None:
        df = pd.DataFrame(rows)
        df = order_columns(df)
        csv_path = f"{base_path}.csv"
        xlsx_path = f"{base_path}.xlsx"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        df.to_excel(xlsx_path, index=False, engine="openpyxl")


def order_columns(df: pd.DataFrame) -> pd.DataFrame:
    preferred_prefixes = [
        "dataset_",
        "place_",
        "longitude",
        "latitude",
        "location_",
        "submission_set_",
        "submission_",
        "attachment_",
    ]

    preferred = []
    remaining = list(df.columns)
    for prefix in preferred_prefixes:
        matches = [col for col in remaining if col == prefix or col.startswith(prefix)]
        for col in matches:
            preferred.append(col)
            remaining.remove(col)

    return df[preferred + sorted(remaining)]


def join_urls_from_attachments(attachments: List[Dict[str, Any]]) -> str:
    urls = [a.get("file") for a in attachments if a.get("file")]
    return " | ".join(urls)


def normalize_for_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return str(value)
    return value


def safe_filename(value: str, max_len: int = 150) -> str:
    value = value.strip().replace("/", "-")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    if not value:
        value = "item"
    return value[:max_len]


def infer_extension(url: str, attachment: Optional[Dict[str, Any]] = None) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}:
        return suffix

    name = (attachment or {}).get("name")
    if isinstance(name, str):
        guessed = mimetypes.guess_extension(name)
        if guessed:
            return guessed

    return ".jpg"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export CartoSpot dataset data to CSV/XLSX plus attachments.")
    parser.add_argument(
        "--dataset-url",
        required=True,
        help="Full dataset endpoint URL, e.g. https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal",
    )
    parser.add_argument(
        "--submission-set",
        required=True,
        help="Submission set name to export per Place, e.g. contributions, inventory, comments",
    )
    parser.add_argument(
        "--output-dir",
        default=str(outputs_dir("exports")),
        help="Directory where export folders and files will be created",
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="Optional API token for authenticated requests if needed",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="Optional delay between requests to be gentle with the API",
    )
    parser.add_argument(
        "--skip-attachment-download",
        action="store_true",
        help="Do not download submission attachments; only export their URLs in CSV/XLSX",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    exporter = CartoSpotExporter(
        dataset_url=args.dataset_url,
        submission_set_name=args.submission_set,
        output_dir=Path(args.output_dir),
        api_token=args.api_token,
        delay_seconds=args.delay_seconds,
        skip_attachment_download=args.skip_attachment_download,
    )
    try:
        exporter.export()
        print("Export completed successfully.")
        return 0
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
