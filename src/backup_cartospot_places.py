#!/usr/bin/env python3
import argparse
import copy
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from project_paths import outputs_dir

# HOW TO USE IT

# python backup_cartospot_places.py \
#   --url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map/places" \
#   --include-submissions

# python backup_cartospot_places.py \
#   --url "https://api.cartospot.com/api/v2/ORGANIZATION/datasets/DATASET/places" \
#   --output "./backups/DATASET"

DEFAULT_URL = (
    "https://api.cartospot.com/api/v2/meitheal/"
    "datasets/loughlinstown-memory-map/places"
)


def create_session():
    """HTTP session with automatic retries."""

    session = requests.Session()

    retries = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retries)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "Accept": "application/json, image/*;q=0.9, */*;q=0.8",
        "User-Agent": "CartoSpot-Place-Backup/1.0",
    })

    return session


def save_json(path, data):
    """Save JSON preserving Unicode."""

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )


def get_next_page(url, metadata):
    """Resolve the next page from CartoSpot metadata."""

    next_page = metadata.get("next")

    if next_page:
        if isinstance(next_page, int):
            return f"{url.split('?')[0]}?page={next_page}"

        if str(next_page).isdigit():
            return f"{url.split('?')[0]}?page={next_page}"

        return urljoin(url, str(next_page))

    page = metadata.get("page")
    num_pages = metadata.get("num_pages")

    if page is not None and num_pages is not None:
        if int(page) < int(num_pages):
            return f"{url.split('?')[0]}?page={int(page) + 1}"

    return None


def fetch_places(session, url, include_submissions=False):
    """Retrieve every page of Place features."""

    features = []
    seen_ids = set()
    visited_urls = set()

    if include_submissions:
        separator = "&" if "?" in url else "?"
        url += f"{separator}include_submissions=true"

    while url:
        if url in visited_urls:
            raise RuntimeError("Pagination loop detected")

        visited_urls.add(url)

        print(f"Fetching: {url}")

        response = session.get(url, timeout=60)
        response.raise_for_status()

        data = response.json()

        if data.get("type") != "FeatureCollection":
            raise ValueError("Unexpected API response")

        for feature in data.get("features", []):

            place_id = feature.get(
                "properties", {}
            ).get("id", feature.get("id"))

            if place_id is not None:
                if place_id in seen_ids:
                    continue
                seen_ids.add(place_id)

            features.append(feature)

        url = get_next_page(
            url,
            data.get("metadata") or {},
        )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def iter_attachments(obj):
    """
    Find attachment dictionaries recursively.

    Supports:
    - Place attachments
    - Attachments inside submissions
    - Nested attachment collections
    """

    if isinstance(obj, dict):

        for key, value in obj.items():

            if key == "attachments" and isinstance(value, list):

                for attachment in value:
                    if (
                        isinstance(attachment, dict)
                        and isinstance(attachment.get("file"), str)
                    ):
                        yield attachment

            else:
                yield from iter_attachments(value)

    elif isinstance(obj, list):

        for item in obj:
            yield from iter_attachments(item)


def detect_extension(response, first_chunk, url):
    """Determine image type, including extensionless blobs."""

    content_type = response.headers.get(
        "Content-Type", ""
    ).split(";")[0].lower()

    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/tiff": ".tif",
        "image/heic": ".heic",
        "image/avif": ".avif",
    }

    if content_type in extensions:
        return extensions[content_type]

    # Detect image type from file signatures.
    if first_chunk.startswith(b"\xff\xd8\xff"):
        return ".jpg"

    if first_chunk.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    if first_chunk.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"

    if (
        first_chunk.startswith(b"RIFF")
        and first_chunk[8:12] == b"WEBP"
    ):
        return ".webp"

    if first_chunk.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"

    if first_chunk[4:8] == b"ftyp":
        if first_chunk[8:12] in (
            b"heic", b"heix", b"hevc", b"mif1"
        ):
            return ".heic"
        if first_chunk[8:12] == b"avif":
            return ".avif"

    # Fallback to the URL extension.
    extension = Path(urlparse(url).path).suffix.lower()

    if extension in extensions.values():
        return extension

    if content_type.startswith("image/"):
        return mimetypes.guess_extension(content_type)

    return None


def safe_id(value):
    """Create a filesystem-safe identifier."""

    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value))


def download_attachment(
    session,
    url,
    place_id,
    index,
    images_dir,
    force=False,
):
    """Download one image using streaming and atomic writes."""

    url = urljoin(DEFAULT_URL, url)

    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL: {url}")

    url_hash = hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()[:10]

    basename = (
        f"place_{safe_id(place_id)}_"
        f"{index:03d}_{url_hash}"
    )

    # Reuse existing image unless --force is enabled.
    if not force:
        for existing in images_dir.glob(f"{basename}.*"):
            if existing.is_file() and existing.stat().st_size > 0:
                return existing, "existing"

    with session.get(
        url,
        timeout=(15, 120),
        stream=True,
    ) as response:

        response.raise_for_status()

        chunks = response.iter_content(chunk_size=1024 * 1024)
        first_chunk = next(chunks, b"")

        if not first_chunk:
            raise ValueError("Empty attachment")

        extension = detect_extension(
            response,
            first_chunk,
            url,
        )

        if not extension:
            return None, "not_image"

        filepath = images_dir / f"{basename}{extension}"
        temporary = images_dir / f"{basename}{extension}.part"

        try:
            with temporary.open("wb") as file:

                file.write(first_chunk)

                for chunk in chunks:
                    if chunk:
                        file.write(chunk)

            temporary.replace(filepath)

        finally:
            temporary.unlink(missing_ok=True)

    return filepath, "downloaded"


def backup(args):

    dataset = urlparse(args.url).path.rstrip("/").split("/")[-2]

    output_dir = Path(args.output).resolve() if args.output else outputs_dir("backups", dataset)
    images_dir = output_dir / "images"

    images_dir.mkdir(parents=True, exist_ok=True)

    session = create_session()

    print("\n=== CARTOSPOT PLACE BACKUP ===\n")
    print(f"Dataset: {dataset}")
    print(f"Output: {output_dir}\n")

    # 1. Retrieve original GeoJSON.
    original = fetch_places(
        session,
        args.url,
        args.include_submissions,
    )

    features = original["features"]

    print(f"\nPlaces retrieved: {len(features)}")

    save_json(
        output_dir / "places.geojson",
        original,
    )

    # 2. Work on an independent copy.
    local = copy.deepcopy(original)

    manifest = []
    errors = []

    downloaded = 0
    existing = 0
    skipped = 0

    # 3. Download images associated with each Place.
    for feature in local["features"]:

        properties = feature.get("properties", {})

        place_id = properties.get(
            "id",
            feature.get("id"),
        )

        attachments = list(iter_attachments(properties))

        if not attachments:
            continue

        print(
            f"\nPlace {place_id}: "
            f"{len(attachments)} attachment(s)"
        )

        for index, attachment in enumerate(
            attachments,
            start=1,
        ):

            image_url = attachment["file"]

            try:

                filepath, status = download_attachment(
                    session=session,
                    url=image_url,
                    place_id=place_id,
                    index=index,
                    images_dir=images_dir,
                    force=args.force,
                )

                if status == "not_image":
                    skipped += 1
                    print(f"  Skipped non-image: {image_url}")
                    continue

                relative_path = filepath.relative_to(output_dir)

                # Add local path, preserving original URL.
                attachment["local_file"] = relative_path.as_posix()

                manifest.append({
                    "place_id": place_id,
                    "place_name": properties.get("name"),
                    "location_type": properties.get("location_type"),
                    "source_url": image_url,
                    "local_file": relative_path.as_posix(),
                    "status": status,
                })

                if status == "downloaded":
                    downloaded += 1
                else:
                    existing += 1

                print(f"  [{status}] {filepath.name}")

            except (requests.RequestException, OSError, ValueError) as error:

                print(f"  ERROR: {error}")

                errors.append({
                    "place_id": place_id,
                    "url": image_url,
                    "error": str(error),
                })

    # 4. Save GeoJSON with local references.
    save_json(
        output_dir / "places_local.geojson",
        local,
    )

    # 5. Save attachment manifest.
    save_json(
        output_dir / "attachments_manifest.json",
        manifest,
    )

    # 6. Save backup report.
    report = {
        "dataset": dataset,
        "source": args.url,
        "total_places": len(features),
        "total_images": downloaded + existing,
        "downloaded": downloaded,
        "already_existing": existing,
        "non_image_attachments": skipped,
        "failed": len(errors),
        "errors": errors,
        "include_submissions": args.include_submissions,
    }

    save_json(
        output_dir / "backup_report.json",
        report,
    )

    print("\n=== BACKUP FINISHED ===")
    print(f"Places: {len(features)}")
    print(f"Images downloaded: {downloaded}")
    print(f"Existing images: {existing}")
    print(f"Non-image attachments: {skipped}")
    print(f"Errors: {len(errors)}")
    print(f"Directory: {output_dir}")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Backup CartoSpot geolocated Place datasets"
    )

    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="CartoSpot Place API endpoint",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Destination directory (default: outputs/backups/<dataset-slug>)",
    )

    parser.add_argument(
        "--include-submissions",
        action="store_true",
        help="Include Place submissions returned by the API",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Download images again, even if already present",
    )

    args = parser.parse_args()

    backup(args)