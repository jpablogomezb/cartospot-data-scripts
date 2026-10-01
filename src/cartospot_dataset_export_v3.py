#!/usr/bin/env python3
"""
CartoSpot dataset export utility (v3).

Purpose
-------
Export a CartoSpot dataset into:
1. Original aggregated GeoJSON (plus untouched individual API pages)
2. One dataset-level Places summary file (CSV + XLSX)
3. Downloaded direct Place attachments and an attachment manifest
4. Optionally, per-Place submissions (CSV + XLSX) and their attachments

Main refinements in v3
----------------------
- Cleaner, stable column ordering for reserved vs dynamic fields
- Normalized submitter fields for both Places and Submissions
- Attachment URL and local file columns on per-submission rows
- Legacy support for `location` with canonical preference for `location_default`
- Optional metadata sheet in XLSX exports

Example
-------
python cartospot_dataset_export_v3.py \
  --dataset-url https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal \
  --submission-set contributions

python cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --skip-place-attachments \
  --output-dir ../outputs
"""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import mimetypes
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import pandas as pd
import requests

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from project_paths import outputs_dir

TIMEOUT = 60
USER_AGENT = "CartoSpot Dataset Export/3.0"

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
        submission_set_name: Optional[str],
        output_dir: Path,
        api_token: Optional[str] = None,
        delay_seconds: float = 0.0,
        skip_attachment_download: bool = False,
        hide_noisy_fields: bool = False,
        skip_place_attachments: bool = False,
        force: bool = False,
    ) -> None:
        self.dataset_url = dataset_url.rstrip("/")
        # Accept either the dataset URL or its /places endpoint.
        if urlsplit(self.dataset_url).path.endswith("/places"):
            self.dataset_url = self.dataset_url[:-len("/places")]
        self.submission_set_name = submission_set_name.strip() if submission_set_name else None
        self.output_dir = output_dir
        self.delay_seconds = delay_seconds
        self.skip_attachment_download = skip_attachment_download
        self.hide_noisy_fields = hide_noisy_fields
        self.skip_place_attachments = skip_place_attachments
        self.force = force
        self.attachment_records: List[Dict[str, Any]] = []
        self.download_errors: List[Dict[str, Any]] = []

        self.session = requests.Session()
        retries = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if api_token:
            self.session.headers.update({"Authorization": f"Token {api_token}"})

    def get_json(self, url: str) -> Dict[str, Any]:
        response = self.session.get(urljoin(self.dataset_url + "/", str(url)), timeout=TIMEOUT)
        response.raise_for_status()
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        return response.json()

    def next_page_url(self, url: str, metadata: Dict[str, Any]) -> Optional[str]:
        """Resolve next URL and tolerate numeric, relative and absolute links."""
        next_value = metadata.get("next")
        if next_value not in (None, "", False):
            if isinstance(next_value, int) or str(next_value).isdigit():
                parts = urlsplit(url)
                params = dict(parse_qsl(parts.query, keep_blank_values=True))
                params["page"] = str(next_value)
                return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
            return urljoin(url, str(next_value))
        page, pages = metadata.get("page"), metadata.get("num_pages")
        if page is not None and pages is not None and int(page) < int(pages):
            parts = urlsplit(url)
            params = dict(parse_qsl(parts.query, keep_blank_values=True))
            params["page"] = str(int(page) + 1)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
        return None

    def get_paginated_results(self, url: str, results_key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        next_url = url
        visited: set[str] = set()
        while next_url:
            if next_url in visited:
                raise RuntimeError(f"Pagination loop at {next_url}")
            visited.add(next_url)
            payload = self.get_json(next_url)
            page_items = payload.get(results_key, [])
            if not isinstance(page_items, list):
                raise ValueError(f"Expected '{results_key}' to be a list at {next_url}")
            items.extend(page_items)
            next_url = self.next_page_url(next_url, payload.get("metadata") or {})
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

    def fetch_places(self, places_url: str, raw_pages_dir: Path) -> List[Dict[str, Any]]:
        """Fetch Places and store each unmodified API response for auditability."""
        places: List[Dict[str, Any]] = []
        next_url = places_url
        visited: set[str] = set()
        raw_pages_dir.mkdir(parents=True, exist_ok=True)
        page_index = 0
        while next_url:
            if next_url in visited:
                raise RuntimeError(f"Pagination loop at {next_url}")
            visited.add(next_url)
            payload = self.get_json(next_url)
            features = payload.get("features", [])
            if not isinstance(features, list):
                raise ValueError(f"Expected 'features' to be a list at {next_url}")
            page_index += 1
            save_json(raw_pages_dir / f"page_{page_index:04d}.json", payload)
            places.extend(features)
            next_url = self.next_page_url(next_url, payload.get("metadata") or {})
        return places

    def download_attachment(self, url: str, target_dir: Path, name_base: str) -> Tuple[Path, str]:
        """Download an attachment atomically, guessing extension from HTTP headers."""
        absolute_url = urljoin(self.dataset_url + "/", url)
        if urlsplit(absolute_url).scheme not in ("http", "https"):
            raise ValueError(f"Unsupported attachment URL: {url}")
        target_dir.mkdir(parents=True, exist_ok=True)
        hash_suffix = hashlib.sha256(absolute_url.encode("utf-8")).hexdigest()[:12]
        stem = safe_filename(f"{name_base}_{hash_suffix}")
        if not self.force:
            for old in target_dir.glob(f"{stem}.*"):
                if old.is_file() and old.stat().st_size > 0 and not old.name.endswith(".part"):
                    return old, "existing"
        with self.session.get(absolute_url, stream=True, timeout=TIMEOUT) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            chunks = response.iter_content(chunk_size=1024 * 1024)
            first_chunk = next(chunks, b"")
            ext = extension_from_signature(first_chunk) or extension_from_type(content_type) or infer_extension(absolute_url)
            path = target_dir / (stem + ext)
            temporary = target_dir / (stem + ext + ".part")
            try:
                with temporary.open("wb") as output:
                    if first_chunk:
                        output.write(first_chunk)
                    for chunk in chunks:
                        if chunk:
                            output.write(chunk)
                if temporary.stat().st_size == 0:
                    raise ValueError(f"Empty response for attachment: {url}")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        return path, "downloaded"

    def download_place_attachments(
        self, dataset: DatasetInfo, place_id: Any, properties: Dict[str, Any], place_folder: Path
    ) -> List[str]:
        """Download only the attachments directly linked to this Place."""
        paths: List[str] = []
        for index, attachment in enumerate(properties.get("attachments") or [], 1):
            if not isinstance(attachment, dict) or not attachment.get("file"):
                continue
            url = attachment["file"]
            try:
                path, status = self.download_attachment(
                    url, place_folder / "attachments",
                    f"place_{place_id}_attachment_{index}",
                )
                relative = path.relative_to(self.dataset_base_dir).as_posix()
                paths.append(relative)
                self.attachment_records.append({
                    "type": "place", "place_id": place_id, "submission_id": None,
                    "attachment_index": index, "source_url": url,
                    "local_file": relative, "status": status,
                })
                print(f"    Place attachment [{status}]: {relative}")
            except (requests.RequestException, OSError, ValueError) as exc:
                error = {"type": "place", "place_id": place_id, "source_url": url, "error": str(exc)}
                self.download_errors.append(error)
                print(f"    Place attachment ERROR: {error}", file=sys.stderr)
        return paths

    def export(self) -> None:
        dataset = self.fetch_dataset_info()
        dataset_base_dir = self.output_dir / safe_filename(f"{dataset.slug}_{dataset.id}")
        dataset_base_dir.mkdir(parents=True, exist_ok=True)

        self.dataset_base_dir = dataset_base_dir
        places = self.fetch_places(dataset.places_url, dataset_base_dir / "api_pages")
        # GeoJSON contains the original Features, unmodified by tabular processing.
        save_json(dataset_base_dir / "places.geojson", {"type": "FeatureCollection", "features": places})
        print(f"Fetched {len(places)} place(s) from dataset '{dataset.slug}'")

        self.place_attachment_files: Dict[Any, List[str]] = {}
        place_summary_rows: List[Dict[str, Any]] = []
        for feature in places:
            row = self.process_place(dataset, feature, dataset_base_dir)
            place_summary_rows.append(row)

        summary_base = dataset_base_dir / safe_filename(f"{dataset.slug}_{dataset.id}_places_summary")
        dataset_metadata = {
            "dataset_id": dataset.id,
            "dataset_slug": dataset.slug,
            "dataset_display_name": dataset.display_name,
            "dataset_url": dataset.url,
            "submission_set_name": self.submission_set_name,
            "place_count": len(places),
        }
        self.write_tabular_exports(
            rows=place_summary_rows,
            base_path=summary_base,
            export_type="places_summary",
            metadata=dataset_metadata,
        )
        print(f"Wrote dataset summary files: {summary_base}.csv and {summary_base}.xlsx")
        save_json(dataset_base_dir / "attachments_manifest.json", self.attachment_records)
        save_json(dataset_base_dir / "backup_report.json", {
            "dataset": dataset.slug, "dataset_id": dataset.id,
            "places": len(places), "submission_set": self.submission_set_name,
            "attachment_files": len(self.attachment_records),
            "attachment_errors": self.download_errors,
        })
        if self.download_errors:
            print(f"WARNING: {len(self.download_errors)} attachment(s) failed; see backup_report.json", file=sys.stderr)


    def process_place(
        self,
        dataset: DatasetInfo,
        feature: Dict[str, Any],
        dataset_base_dir: Path,
    ) -> Dict[str, Any]:
        properties = feature.get("properties", {}) or {}
        place_id = properties.get("id") or feature.get("id")
        if place_id is None:
            raise ValueError("Place is missing an id")

        place_name = get_place_display_name(properties, place_id)
        safe_place_name = safe_filename(str(place_name), max_len=80)
        place_folder = dataset_base_dir / safe_filename(
            f"{dataset.slug}_{dataset.id}_place_{place_id}_{safe_place_name}"
        )
        place_folder.mkdir(parents=True, exist_ok=True)

        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or [None, None]
        lon = coordinates[0] if len(coordinates) > 0 else None
        lat = coordinates[1] if len(coordinates) > 1 else None

        place_attachment_files: List[str] = []
        if not self.skip_place_attachments:
            place_attachment_files = self.download_place_attachments(
                dataset, place_id, properties, place_folder
            )
        self.place_attachment_files[place_id] = place_attachment_files

        submission_set_info = (
            (properties.get("submission_sets") or {}).get(self.submission_set_name)
            if self.submission_set_name else None
        )
        submission_count = 0
        submissions: List[Dict[str, Any]] = []
        submissions_file_base: Optional[Path] = None
        latest_submission_id = None
        latest_submission_datetime = None
        latest_attachment_url = None

        if submission_set_info and submission_set_info.get("url"):
            submissions = self.get_paginated_results(submission_set_info["url"], "results")
            submission_count = len(submissions)
            submissions_file_base = place_folder / safe_filename(
                f"{dataset.slug}_{dataset.id}_place_{place_id}_{safe_place_name}_{self.submission_set_name}"
            )

            attachment_map: Dict[Tuple[Any, int], str] = {}
            if not self.skip_attachment_download:
                attachment_map = self.download_submission_attachments(
                    dataset=dataset,
                    place_id=int(place_id),
                    place_name=safe_place_name,
                    place_folder=place_folder,
                    submissions=submissions,
                )

            submission_rows = self.build_submission_rows(
                dataset=dataset,
                feature=feature,
                submissions=submissions,
                attachment_map=attachment_map,
            )
            place_metadata = {
                "dataset_id": dataset.id,
                "dataset_slug": dataset.slug,
                "place_id": place_id,
                "place_name": place_name,
                "submission_set_name": self.submission_set_name,
                "submission_count": submission_count,
                "place_url": properties.get("url"),
            }
            self.write_tabular_exports(
                rows=submission_rows,
                base_path=submissions_file_base,
                export_type="submissions",
                metadata=place_metadata,
            )
            print(
                f"  Place {place_id}: wrote {len(submission_rows)} {self.submission_set_name} row(s) "
                f"to {submissions_file_base}.csv/.xlsx"
            )

            latest_submission = max(
                (s for s in submissions if s.get("created_datetime")),
                key=lambda x: x.get("created_datetime"),
                default=None,
            )
            if latest_submission:
                latest_submission_id = latest_submission.get("id")
                latest_submission_datetime = latest_submission.get("created_datetime")
                latest_attachments = latest_submission.get("attachments") or []
                if latest_attachments:
                    latest_attachment_url = latest_attachments[0].get("file")
        elif self.submission_set_name:
            print(
                f"  Place {place_id}: no '{self.submission_set_name}' submission set found; "
                f"only summary row will be exported"
            )

        return self.build_place_summary_row(
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
            latest_submission_id=latest_submission_id,
            latest_submission_datetime=latest_submission_datetime,
            latest_attachment_url=latest_attachment_url,
        )

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
        latest_submission_id: Any,
        latest_submission_datetime: Any,
        latest_attachment_url: Any,
    ) -> Dict[str, Any]:
        properties = feature.get("properties", {}) or {}
        place_attachments = properties.get("attachments") or []
        submitter_info = normalize_submitter_fields(properties.get("submitter"), properties.get("submitter_name"))
        submission_dates = [s.get("created_datetime") for s in submissions if s.get("created_datetime")]

        row: Dict[str, Any] = {
            "dataset_id": dataset.id,
            "dataset_slug": dataset.slug,
            "dataset_display_name": dataset.display_name,
            "dataset_url": dataset.url,
            "place_id": place_id,
            "place_name": place_name,
            "location_default": properties.get("location_default") or properties.get("location"),
            "location_type": properties.get("location_type"),
            "visible": properties.get("visible"),
            "created_datetime": properties.get("created_datetime"),
            "updated_datetime": properties.get("updated_datetime"),
            "submitter_type": submitter_info["submitter_type"],
            "submitter_display": submitter_info["submitter_display"],
            "submitter_username": submitter_info["submitter_username"],
            "submitter_id": submitter_info["submitter_id"],
            "user_token": properties.get("user_token"),
            "longitude": lon,
            "latitude": lat,
            "place_url": properties.get("url"),
            "submission_set_name": self.submission_set_name,
            "submission_set_url": submission_set_url,
            "submission_count": submission_count,
            "first_submission_created_datetime": min(submission_dates) if submission_dates else None,
            "latest_submission_datetime": latest_submission_datetime,
            "latest_submission_id": latest_submission_id,
            "latest_attachment_url": latest_attachment_url,
            "place_attachment_count": len(place_attachments),
            "place_attachment_urls": join_urls_from_attachments(place_attachments),
            "place_attachment_local_files": " | ".join(self.place_attachment_files.get(place_id, [])) or None,
            "submissions_csv_path": f"{submissions_file_base}.csv" if submissions_file_base else None,
            "submissions_xlsx_path": f"{submissions_file_base}.xlsx" if submissions_file_base else None,
        }

        for key, value in properties.items():
            if key in PLACE_RESERVED_KEYS:
                continue
            if self.hide_noisy_fields and key in NOISY_RENDER_KEYS:
                continue
            row[f"place_custom__{key}"] = normalize_for_cell(value)

        return row

    def build_submission_rows(
        self,
        dataset: DatasetInfo,
        feature: Dict[str, Any],
        submissions: List[Dict[str, Any]],
        attachment_map: Optional[Dict[Tuple[Any, int], str]] = None,
    ) -> List[Dict[str, Any]]:
        properties = feature.get("properties", {}) or {}
        place_id = properties.get("id") or feature.get("id")
        place_name = get_place_display_name(properties, place_id)
        rows: List[Dict[str, Any]] = []
        attachment_map = attachment_map or {}

        for submission in submissions:
            attachments = submission.get("attachments") or []
            submitter_info = normalize_submitter_fields(
                submission.get("submitter"),
                submission.get("submitter_name"),
            )
            row: Dict[str, Any] = {
                "dataset_id": dataset.id,
                "dataset_slug": dataset.slug,
                "dataset_display_name": dataset.display_name,
                "submission_set_name": self.submission_set_name,
                "place_id": place_id,
                "place_name": place_name,
                "submission_id": submission.get("id"),
                "submitter_type": submitter_info["submitter_type"],
                "submitter_display": submitter_info["submitter_display"],
                "submitter_username": submitter_info["submitter_username"],
                "submitter_id": submitter_info["submitter_id"],
                "visible": submission.get("visible"),
                "created_datetime": submission.get("created_datetime"),
                "updated_datetime": submission.get("updated_datetime"),
                "dataset_url": submission.get("dataset") or dataset.url,
                "set_url": submission.get("set"),
                "place_url": submission.get("place") or properties.get("url"),
                "submission_url": submission.get("url"),
                "user_token": submission.get("user_token"),
                "attachment_count": len(attachments),
                "attachment_urls": join_urls_from_attachments(attachments),
                "downloaded_attachment_files": " | ".join(
                    attachment_map[(submission.get("id"), idx)]
                    for idx in range(1, len(attachments) + 1)
                    if (submission.get("id"), idx) in attachment_map
                ) or None,
            }

            for idx, attachment in enumerate(attachments, start=1):
                row[f"attachment_{idx}_url"] = attachment.get("file")
                row[f"attachment_{idx}_name"] = attachment.get("name")
                row[f"attachment_{idx}_width"] = attachment.get("width")
                row[f"attachment_{idx}_height"] = attachment.get("height")
                row[f"attachment_{idx}_local_file"] = attachment_map.get((submission.get("id"), idx))

            for key, value in submission.items():
                if key in SUBMISSION_RESERVED_KEYS:
                    continue
                if self.hide_noisy_fields and key in NOISY_RENDER_KEYS:
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
    ) -> Dict[Tuple[Any, int], str]:
        attachments_dir = place_folder / "submission_attachments"
        attachment_map: Dict[Tuple[Any, int], str] = {}
        for submission in submissions:
            submission_id = submission.get("id")
            for idx, attachment in enumerate(submission.get("attachments") or [], start=1):
                file_url = attachment.get("file") if isinstance(attachment, dict) else None
                if not file_url:
                    continue
                try:
                    path, status = self.download_attachment(
                        file_url, attachments_dir,
                        f"place_{place_id}_{safe_filename(str(self.submission_set_name))}_submission_{submission_id}_attachment_{idx}",
                    )
                    relative = path.relative_to(self.dataset_base_dir).as_posix()
                    attachment_map[(submission_id, idx)] = relative
                    self.attachment_records.append({
                        "type": "submission", "place_id": place_id,
                        "submission_id": submission_id,
                        "submission_set": self.submission_set_name,
                        "attachment_index": idx, "source_url": file_url,
                        "local_file": relative, "status": status,
                    })
                    print(f"    Submission attachment [{status}]: {relative}")
                except (requests.RequestException, OSError, ValueError) as exc:
                    error = {"type": "submission", "place_id": place_id,
                             "submission_id": submission_id, "source_url": file_url, "error": str(exc)}
                    self.download_errors.append(error)
                    print(f"    Submission attachment ERROR: {error}", file=sys.stderr)
        return attachment_map

    @staticmethod
    def write_tabular_exports(
        rows: List[Dict[str, Any]],
        base_path: Path,
        export_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        df = pd.DataFrame(rows)
        df = order_columns(df, export_type=export_type)
        csv_path = f"{base_path}.csv"
        xlsx_path = f"{base_path}.xlsx"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="data")
            if metadata:
                meta_df = pd.DataFrame(
                    [{"key": key, "value": normalize_for_cell(value)} for key, value in metadata.items()]
                )
                meta_df.to_excel(writer, index=False, sheet_name="metadata")


def get_place_display_name(properties: Dict[str, Any], place_id: Any) -> str:
    return (
        properties.get("name")
        or properties.get("location_default")
        or properties.get("location")
        or f"place_{place_id}"
    )


def normalize_submitter_fields(submitter: Any, submitter_name: Any = None) -> Dict[str, Any]:
    if isinstance(submitter, dict):
        return {
            "submitter_type": "user",
            "submitter_display": submitter.get("name"),
            "submitter_username": submitter.get("username"),
            "submitter_id": submitter.get("id"),
        }
    if submitter_name:
        return {
            "submitter_type": "anonymous",
            "submitter_display": submitter_name,
            "submitter_username": None,
            "submitter_id": None,
        }
    return {
        "submitter_type": None,
        "submitter_display": None,
        "submitter_username": None,
        "submitter_id": None,
    }


def order_columns(df: pd.DataFrame, export_type: str) -> pd.DataFrame:
    columns = list(df.columns)

    if export_type == "places_summary":
        preferred = [
            "dataset_id",
            "dataset_slug",
            "dataset_display_name",
            "dataset_url",
            "place_id",
            "place_name",
            "location_default",
            "location_type",
            "visible",
            "created_datetime",
            "updated_datetime",
            "submitter_type",
            "submitter_display",
            "submitter_username",
            "submitter_id",
            "user_token",
            "longitude",
            "latitude",
            "place_url",
            "submission_set_name",
            "submission_set_url",
            "submission_count",
            "first_submission_created_datetime",
            "latest_submission_datetime",
            "latest_submission_id",
            "latest_attachment_url",
            "place_attachment_count",
            "place_attachment_urls",
            "place_attachment_local_files",
            "submissions_csv_path",
            "submissions_xlsx_path",
        ]
        prefix_order = ["place_custom__"]
    elif export_type == "submissions":
        preferred = [
            "dataset_id",
            "dataset_slug",
            "dataset_display_name",
            "submission_set_name",
            "place_id",
            "place_name",
            "submission_id",
            "submitter_type",
            "submitter_display",
            "submitter_username",
            "submitter_id",
            "visible",
            "created_datetime",
            "updated_datetime",
            "dataset_url",
            "set_url",
            "place_url",
            "submission_url",
            "user_token",
            "attachment_count",
            "attachment_urls",
            "downloaded_attachment_files",
        ]
        prefix_order = ["attachment_", "submission_custom__"]
    else:
        preferred = []
        prefix_order = []

    ordered: List[str] = [c for c in preferred if c in columns]
    remaining = [c for c in columns if c not in ordered]

    for prefix in prefix_order:
        matches = sorted([c for c in remaining if c.startswith(prefix)])
        ordered.extend(matches)
        remaining = [c for c in remaining if c not in matches]

    ordered.extend(sorted(remaining))
    return df[ordered]


def join_urls_from_attachments(attachments: List[Dict[str, Any]]) -> str:
    urls = [a.get("file") for a in attachments if a.get("file")]
    return " | ".join(urls)


def normalize_for_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def safe_filename(value: str, max_len: int = 150) -> str:
    value = value.strip().replace("/", "-")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    if not value:
        value = "item"
    return value[:max_len]


def extension_from_signature(first_chunk: bytes) -> Optional[str]:
    """Detect image/PDF formats when CartoSpot serves a generic -blob URL."""
    if first_chunk.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if first_chunk.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if first_chunk.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if first_chunk.startswith(b"RIFF") and first_chunk[8:12] == b"WEBP":
        return ".webp"
    if first_chunk.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if first_chunk.startswith(b"%PDF-"):
        return ".pdf"
    if first_chunk[4:8] == b"ftyp":
        if first_chunk[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
            return ".heic"
        if first_chunk[8:12] == b"avif":
            return ".avif"
    return None


def extension_from_type(content_type: str) -> Optional[str]:
    known = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "image/tiff": ".tif", "image/heic": ".heic",
        "image/avif": ".avif", "application/pdf": ".pdf",
    }
    if content_type in known:
        return known[content_type]
    if content_type in ("", "application/octet-stream"):
        return None
    guessed = mimetypes.guess_extension(content_type)
    return guessed if guessed and len(guessed) < 12 else None


def infer_extension(url: str, attachment: Optional[Dict[str, Any]] = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
                  ".bmp", ".heic", ".avif", ".pdf", ".mp4", ".mov", ".mp3", ".wav"}:
        return suffix
    if attachment:
        filename = attachment.get("name")
        if isinstance(filename, str):
            guessed = Path(filename).suffix.lower()
            if guessed and re.fullmatch(r"\.[a-z0-9]{1,10}", guessed):
                return guessed
    return ".bin"


def save_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export CartoSpot dataset data to CSV/XLSX plus attachments.")
    parser.add_argument(
        "--dataset-url",
        required=True,
        help="Full dataset endpoint URL, e.g. https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal",
    )
    parser.add_argument(
        "--submission-set",
        required=False,
        default=None,
        help="Optional: export this submission set per Place; omit to export Places only",
    )
    parser.add_argument(
        "--output-dir",
        default=str(outputs_dir("exports")),
        help="Directory where export folders and files will be created (default: PROJECT_ROOT/outputs/exports)",
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
    parser.add_argument(
        "--hide-noisy-fields",
        action="store_true",
        help="Hide known technical fields like csrfmiddlewaretoken from exported custom columns",
    )
    parser.add_argument(
        "--skip-place-attachments", action="store_true",
        help="Export Place data but do not download direct Place attachments",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Redownload attachments even when local copies exist",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    exporter = CartoSpotExporter(
        dataset_url=args.dataset_url,
        submission_set_name=args.submission_set,
        output_dir=Path(args.output_dir),
        api_token=args.api_token or os.environ.get("CARTOSPOT_API_TOKEN"),
        delay_seconds=args.delay_seconds,
        skip_attachment_download=args.skip_attachment_download,
        hide_noisy_fields=args.hide_noisy_fields,
        skip_place_attachments=args.skip_place_attachments,
        force=args.force,
    )

    try:
        exporter.export()
        if exporter.download_errors:
            print("Export completed with attachment download errors.", file=sys.stderr)
            return 2
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
