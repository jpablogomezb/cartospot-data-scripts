# CartoSpot Dataset Export v3

A command-line utility for exporting geolocated **Place** datasets from the CartoSpot API. It retrieves Places, preserves their geometries and attributes, downloads files attached directly to Places, and can optionally export a selected **SubmissionSet** and its attachments.

> **Default behavior:** exports Places, their direct attachments, and GeoJSON/CSV/XLSX files. **Submissions are not requested or downloaded** unless you specify `--submission-set`.

## Requirements

- Python 3.9 or later.
- HTTP(S) access to the relevant CartoSpot API endpoint.
- Python packages: `requests`, `pandas`, and `openpyxl`.
- A local `project_paths.py` module, importable by the script, providing `outputs_dir()`.

### Recommended project layout

```text
cartospot-scripts/
├── src/
│   ├── cartospot_dataset_export_v3.py
│   └── project_paths.py
├── outputs/
│   └── exports/
└── .venv/
```

The exporter uses the project's existing `project_paths.py`:

```python
from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve().parent
    if here.name == "src":
        return here.parent
    return here


def outputs_dir(*parts: str) -> Path:
    path = project_root().joinpath("outputs", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
```

When both Python files are under `src/`, the default destination is `<project-root>/outputs/exports/`. You can override it using `--output-dir`.

## Installation

Run these commands from the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate    # macOS / Linux
python -m pip install requests pandas openpyxl
```

If you already have a virtual environment with the required packages, you can reuse it.

## Quick start

**Important:** pass the **dataset API URL**, not the public-facing map URL. The script also accepts a dataset URL ending in `/places` and removes that suffix before fetching dataset information.

### 1. Export Places and photos, without Submissions

Example: *Loughlinstown Memory Map*.

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map"
```

This retrieves every Place returned by the API, preserves each Place's geometry and properties, downloads accessible files attached directly to Places, and creates GeoJSON, CSV, and Excel exports. **No Submissions are fetched.**

### 2. Export Places and a selected SubmissionSet

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --submission-set contributions
```

`contributions` is **only an example**: replace it with the actual `SubmissionSet` name associated with Places in your dataset. When supplied, the script additionally requests that set for each Place, writes submission CSV/XLSX files, and downloads associated submission attachments. Places without the requested set are still included in the dataset-level summary.

To inspect the submission sets exposed on the first Place (requires `jq`):

```bash
curl -fsSL \
  "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map/places" \
  | jq '.features[0].properties.submission_sets'
```

The first Place may not contain every set used elsewhere in the dataset. Inspect additional entries in `features` if needed.

### 3. Export data without downloading attachments

To export Places only, with no downloaded files:

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --skip-place-attachments
```

Because `--submission-set` is omitted, this also skips Submissions. Original attachment URLs remain in the exported data.

If you **are** exporting Submissions and want to skip **both** kinds of attachments:

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal" \
  --submission-set contributions \
  --skip-place-attachments \
  --skip-attachment-download
```

### 4. Choose a destination directory

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --output-dir "./backups"
```

Within the chosen directory, the exporter creates a subdirectory named `<dataset-slug>_<dataset-id>`.

### 5. Reuse or refresh downloaded files

When rerun against the same output directory, the exporter reuses existing attachment files with matching names and a nonzero size. **This does not verify that the local file is identical to the remote copy.**

To download attachments again:

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --force
```

Tabular and JSON output files are regenerated on each run.

## Command-line options

| Option | Description |
| --- | --- |
| `--dataset-url URL` | **Required.** CartoSpot dataset API endpoint, normally without `/places`. |
| `--submission-set NAME` | **Optional.** Export only the named SubmissionSet for each Place. Omit it to export Places without requesting Submissions. |
| `--output-dir PATH` | Export destination. Defaults to `outputs_dir("exports")` from `project_paths.py`. |
| `--skip-place-attachments` | Do not download files attached directly to Places. |
| `--skip-attachment-download` | Do not download Submission attachments; this does **not** affect direct Place attachments. |
| `--force` | Download attachments again even when matching local files already exist. |
| `--delay-seconds N` | Wait `N` seconds between requests; default: `0`. |
| `--hide-noisy-fields` | Exclude selected technical fields (such as `csrfmiddlewaretoken`) from dynamic CSV/XLSX columns; the GeoJSON remains unchanged. |
| `--api-token TOKEN` | Optional API token, sent as `Authorization: Token TOKEN`. |
| `--help` | Display the command-line help. |

For the built-in help:

```bash
python src/cartospot_dataset_export_v3.py --help
```

## Output structure

Illustrative output for a dataset containing Place ID `123` (actual dataset IDs and filenames depend on the API):

```text
outputs/
└── exports/
    └── loughlinstown-memory-map_<dataset-id>/
        ├── places.geojson
        ├── loughlinstown-memory-map_<dataset-id>_places_summary.csv
        ├── loughlinstown-memory-map_<dataset-id>_places_summary.xlsx
        ├── attachments_manifest.json
        ├── backup_report.json
        ├── api_pages/
        │   ├── page_0001.json
        │   └── ...
        └── loughlinstown-memory-map_<dataset-id>_place_123_<name>/
            ├── attachments/
            │   └── place_123_attachment_1_<hash>.jpg
            ├── submission_attachments/          # If applicable
            │   └── ...
            ├── ..._contributions.csv            # With --submission-set only
            └── ..._contributions.xlsx           # With --submission-set only
```

### Main export files

- **`places.geojson`** — an aggregated GeoJSON `FeatureCollection` containing the original Place `features` across all API pages, with their `geometry` and `properties` preserved. Full per-page responses, including pagination metadata, are stored under `api_pages/`.
- **`api_pages/page_XXXX.json`** — the JSON response for each page of Places, retained for auditing or reference.
- **`*_places_summary.csv` and `.xlsx`** — one row per Place, including separate `longitude` and `latitude` columns, metadata, attachment references, and custom fields prefixed with `place_custom__`. The Excel workbook has `data` and `metadata` worksheets.
- **`attachments/`** — files referenced directly by a Place's `properties.attachments`.
- **`*_<submission-set>.csv` and `.xlsx`** — one row per Submission in the chosen set, including custom fields prefixed with `submission_custom__` and attachment references.
- **`submission_attachments/`** — files attached to Submissions, when requested.
- **`attachments_manifest.json`** — a record of downloaded or reused attachments, including their source (`place` or `submission`), identifiers, original URL, local path, and download status.
- **`backup_report.json`** — export summary with the Place count, selected SubmissionSet, attachment counts, and any attachment-download errors.

Original image URLs are retained in the GeoJSON and tabular exports; the manifest provides the corresponding local paths.

## Authentication

For authenticated endpoints, you can supply `--api-token`. To avoid putting credentials into your shell history, the exporter also accepts the `CARTOSPOT_API_TOKEN` environment variable:

```bash
export CARTOSPOT_API_TOKEN="YOUR_TOKEN"
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map"
```

Providing a token does **not** automatically include invisible or private records. The exported content depends on API permissions and the API response. This script does not add separate query parameters to request private or invisible data.

## Progress and error handling

The script prints progress information, including the number of Places retrieved and the files written. If an attachment cannot be downloaded, it records the error in `backup_report.json` and continues with other attachments.

Exit codes:

- `0` — export completed with no recorded attachment-download errors.
- `1` — a general or HTTP error prevented the export from completing.
- `2` — the export completed, but one or more attachments could not be downloaded.

Downloads use HTTP retries, streaming, and temporary `.part` files that are renamed after completion. File types are inferred from file signatures, the `Content-Type` header, or the URL. Unidentified file types may use the `.bin` extension.

## Scope and limitations

- The tool is an **exporter**; it does not modify remote CartoSpot records.
- It exports the Places **returned by the API**, not necessarily every record stored in the underlying database (including invisible or private records).
- `--submission-set` selects **one SubmissionSet per run**; it does not automatically export all available sets.
- Direct Place attachments and Submission attachments are handled separately. The script does not follow arbitrary links contained in custom form responses.
- `places.geojson` aggregates the original Place features. For the complete JSON of each API page, use `api_pages/`.
- CSV/XLSX files are designed for tabular analysis and do not replace GeoJSON as a complete geographic representation.
- This is not a full backup of the Django application configuration (for example, all `PlaceType` or `PlaceQuestion` records).
- The manifest records download failures but does not checksum-verify reused files.
- Exported datasets may contain user names, comments, or other personal information. Store them with appropriate access controls.

## Additional example: SCORE Photo Booth

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal" \
  --submission-set contributions \
  --output-dir "./outputs/exports" \
  --delay-seconds 0.3
```

This exports Places and, wherever the `contributions` set exists, its Submissions and their attachments.

---

**Script:** `src/cartospot_dataset_export_v3.py`  
**Path helper:** `src/project_paths.py`  
**Default output directory:** `outputs/exports/`
