# INTERVAL: API download, split processing and GIS exports

- [Notebook](../src/INTERVAL_process_data_from_API_and_generated_files.ipynb)
- [Matching script](../src/INTERVAL_process_data_from_API_and_generated_files.py)

This is the second INTERVAL workflow. It downloads the raw Places/Submissions CSV
ZIP for a dataset, preserves all Places, and separates Places with multiple
Submissions from those with zero or one. It writes CSVs for both groups and
GeoJSON/Shapefile for the zero-or-one group, then packages the results in a ZIP.
The notebook imports the script's implementation, so both interfaces use the same
rules. It does not choose the latest Submission or download attachment images.

## Difference from the local CSV workflow

| Behavior | Local CSV workflow | This API split workflow |
| --- | --- | --- |
| Input | Two local CSV files | API ZIP, or saved ZIP with `--source-zip` |
| Join | Inner | Left; preserves Places without Submissions |
| Multiple Submissions | Separate CSV | Separate CSV; excluded from GIS exports |
| Zero Submissions | Excluded | Included in zero-or-one CSV and GIS exports |
| Species `Other` | Unchanged | Replaced by a nonblank `other_species` response |
| Outputs | Two CSVs | Raw CSVs, processed CSVs, GeoJSON, Shapefile, report, ZIP |

The historical filename `data_valid_submissions_...` is retained for compatibility.
**Here, “valid” means zero or one Submission; it is not a scientific quality
assessment.** The Python result names this group `zero_or_one`.

## Installation

Use Python 3.10 or later. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-interval-api.txt
```

Fiona writes Shapefiles using an explicit schema, including for empty datasets.
This implementation does not need GeoPandas or the `geojson` package. The broader
repository requirements still support the other scripts and notebooks.

For interactive notebooks and the optional real-kernel smoke test:

```bash
python -m pip install jupyterlab nbclient ipykernel
python -m ipykernel install --user --name cartospot-data-scripts --display-name "CartoSpot data scripts"
python -m jupyterlab
```

Select the **CartoSpot data scripts** kernel. Start Jupyter from the repository
root or `src/`. Restart the kernel after changes to the Python module.

## Run against the API

Replace `306` with your dataset ID:

```bash
python src/INTERVAL_process_data_from_API_and_generated_files.py --dataset-id 306
```

The endpoint is:

```text
https://api.cartospot.com/api/v2/db/cartospot/generate-csv/interval/<dataset-id>/
```

The downloader uses GET, a 10-second connection timeout and a 60-second read
inactivity timeout. Its retry policy allows up to three retries for eligible
failures, including HTTP 429/500/502/503/504, with backoff. This is not an overall
60-second deadline. Compressed and declared uncompressed ZIP sizes are each
limited to 100 MiB. The script does not supply authentication credentials.
A 401/403 response needs an authorized export provided separately or a future
explicit authentication enhancement; it is not treated as an empty dataset.

## Process a previously downloaded ZIP

```bash
python src/INTERVAL_process_data_from_API_and_generated_files.py \
  --dataset-id 306 \
  --source-zip /path/to/interval_export.zip \
  --output-dir outputs/my_interval_exports
```

The ZIP must contain exactly one `places.csv` and one `submissions.csv`, at its
root or under directories. Duplicate matches, unsafe paths and incomplete ZIPs
are rejected. Only the two named CSVs are copied to the run directory; arbitrary
ZIP paths are never extracted. Raw CSV bytes are preserved in the final bundle.

| Option | Meaning |
| --- | --- |
| `--dataset-id N` | Required positive dataset ID, including in offline mode. |
| `--source-zip PATH` | Read this ZIP instead of requesting the API. |
| `--output-dir PATH` | Output root; defaults to `<project-root>/outputs/interval_api_split`. |
| `--skip-shapefile` | Generate CSV, GeoJSON, report and ZIP without a Shapefile. |
| `--help` | Show command-line usage. |

Explicit relative paths are relative to the terminal's current directory.
Exit status is `0` on success, `1` for handled processing/download/export errors,
and `2` for invalid command-line arguments.

## Input columns and validation

Column names are case-sensitive; input is comma-separated UTF-8, optionally with
a BOM. The schema follows the original notebook's selected fields:

| Input | Required columns |
| --- | --- |
| Places | `Id`, `location_type`, `tree_privacy`, `NTM_ID`, `tree_height`, `avg_tree_height`, `canopy_perimeter`, `canopy_area`, `geometry_x`, `geometry_y`, `dataset_id`, `dataset_name`, `url` |
| Submissions | `Id`, `location_id`, `does_a_tree_exist_here`, `species`, `other_species`, `tree_type`, `health`, `circumference`, `dbh`, `notes`, `image_url`, `submitter_name` |

Extra columns are omitted from processed outputs but retained in the raw CSVs.
Header-only files are accepted. Missing required columns, duplicate headers,
duplicate Place/Submission IDs and invalid IDs stop processing.

ID validation reuses the first workflow's helpers: IDs are positive integers,
whitespace and leading zeros are normalized, and `12.0` is accepted as `12`.
`Places.dataset_id` must match the requested dataset ID. IDs are kept as text to
avoid rounding large values. This check cannot establish the provenance of a
Submissions CSV without a corresponding dataset field.

The merge uses `Places.Id` → `Submissions.location_id`, with one-to-many validation.
Places without Submissions remain as one row with missing Submission attributes.
Submissions whose Place is absent are excluded from processed outputs and counted
as `unmatched_submissions`. They remain in the original Submissions CSV.

If `species` is exactly `Other`, a nonblank `other_species` replaces it after
trimming whitespace. Blank/whitespace-only responses leave `Other` unchanged.
No broader species normalization or scientific validation is performed.

## Coordinates, types and Shapefile limits

`geometry_x` is longitude and `geometry_y` latitude in WGS84 (EPSG:4326).
Coordinates are converted to numbers and checked for finiteness and ranges
[-180, 180] / [-90, 90]. A missing, nonnumeric or out-of-range coordinate produces
**null geometry**, retaining the Place and its attributes. A valid `(0, 0)` is
accepted. The report lists affected Place IDs in `invalid_geometry_place_ids`.
This geometry check applies to the zero-or-one GIS group only; multiple-Submission
records remain in CSV and are not geometry-validated by this workflow.

Raw/processed CSV attributes are read as text to preserve exact IDs and responses
such as `NA`. GeoJSON properties likewise preserve nonblank source text; missing
or blank attributes become JSON `null`. Geometry coordinates are numeric. When
analyzing measurements, convert the relevant attribute columns to numeric types
explicitly. `location_id` becomes the GeoJSON Feature ID (a string).

Shapefile attribute fields use text with a maximum width of 254 UTF-8 bytes.
IDs are strings too. If an attribute exceeds that limit, the export stops rather
than silently truncating it. Run with `--skip-shapefile` to keep full values in
CSV and GeoJSON. Long URLs and notes are common reasons to use this option.

The Shapefile uses the following shortened field names; other names are unchanged:

| Source | Shapefile |
| --- | --- |
| `location_id` | `loc_id` |
| `location_type` | `loc_type` |
| `tree_privacy` | `privacy` |
| `tree_height` | `height` |
| `avg_tree_height` | `avg_height` |
| `canopy_perimeter` | `canopy_per` |
| `canopy_area` | `canopy_are` |
| `dataset_name` | `dataset` |
| `location_url` | `loc_url` |
| `submission_id` | `sub_id` |
| `does_a_tree_exist_here` | `tree_exist` |
| `circumference` | `circumf` |
| `submitter_name` | `submitter` |

Empty groups generate header-only CSVs, an empty GeoJSON FeatureCollection and
an empty Shapefile with its schema. Null geometries remain records in the Shapefile.

## Output layout and repeated runs

Each run is isolated beneath the selected output root:

```text
outputs/interval_api_split/dataset_id_306/run_<UTC-time>_<unique-suffix>/
  csv_files/
    places.csv
    submissions.csv
    data_valid_submissions_dataset_id_306.csv
    data_multiple_submissions_dataset_id_306.csv
  geofiles/
    data_valid_submissions_dataset_id_306.geojson
    data_valid_submissions_dataset_id_306.shp
    data_valid_submissions_dataset_id_306.shx
    data_valid_submissions_dataset_id_306.dbf
    data_valid_submissions_dataset_id_306.prj
    data_valid_submissions_dataset_id_306.cpg
  processing_report.json
  dataset_id_306_outputs.zip
```

The ZIP stores those data/report files at its root. Shapefile components are
omitted when disabled. The report records group sizes, excluded Submissions,
Places without Submissions, invalid geometry IDs, source, UTC time and Shapefile
status/field mapping. An offline report includes the absolute source ZIP path.

**Output paths have changed from the old notebook:** files are no longer written
into a shared `outputs/csv_files` or `outputs/geofiles` folder. Existing runs are
not overwritten. A temporary directory is renamed only after all exports finish;
handled failures remove that temporary run. A forced process termination can
leave a `.pending_*` directory, which is not a completed export.

## Notebook use

1. Open the notebook and choose the configured kernel.
2. Set `DATASET_ID`, `SOURCE_ZIP`, `OUTPUT_DIR` and `SKIP_SHAPEFILE`.
3. Run all cells. The pipeline cell downloads/processes/writes the full export.
4. Inspect `report`, the source previews and both processed groups.
5. Use the printed ZIP path to find the bundle.

The notebook is an interactive interface over the script, rather than an
independent implementation. Each rerun of the pipeline cell creates a new run.

## Synthetic demonstration and tests

Create an offline ZIP from the included synthetic fixtures:

```bash
python -m zipfile -c /tmp/interval_api_demo.zip \
  tests/fixtures/interval_api_split/places.csv \
  tests/fixtures/interval_api_split/submissions.csv
python src/INTERVAL_process_data_from_API_and_generated_files.py \
  --dataset-id 306 --source-zip /tmp/interval_api_demo.zip
```

Expected: four Places, four Submissions, three zero-or-one Places, one
multiple-Submission Place represented by two rows, two Places without Submissions,
one unmatched Submission and null geometry for Place 4. This is synthetic data.

Run the complete regression suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

API split tests cover grouping, species cleanup, schema/ID validation, malformed
and nested ZIPs, HTTP errors/timeouts/limits, null geometries, actual Shapefile
readback, empty/all-multiple datasets, long attributes, failed-run cleanup,
repeated-run isolation and command-line execution. The optional notebook test
uses a real Jupyter kernel from both the root and `src/`, then compares CSV and
GeoJSON outputs with the script. It is skipped if nbclient/ipykernel are absent.

Tests use local synthetic ZIPs and mocked HTTP responses. They do not confirm
availability or the current schema of the live CartoSpot endpoint. A real dataset
run remains the final integration check before relying on an export.
