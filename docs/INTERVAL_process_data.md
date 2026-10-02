# INTERVAL: process local Places and Submissions CSVs

This guide covers only the local CSV workflow:

- [Notebook](../src/INTERVAL_process_data.ipynb): interactive loading, inspection and export.
- [Python script](../src/INTERVAL_process_data.py): the same processing functions and a command-line interface.

Use this workflow to separate Places with **exactly one Submission** from Places
with **multiple Submissions**, keeping every matching Submission in its group.
It does not download data, select the latest Submission, clean species names,
download images, or generate GeoJSON/Shapefiles. The two API notebooks provide
different workflows.

## Processing rules

1. Read an existing pair of raw INTERVAL `places.csv` and `submissions.csv` files.
2. Validate required columns and positive integer IDs. Place IDs and Submission
   IDs must each be unique within their own input table.
3. Keep the seven Place columns listed below, plus every Submission column.
4. Inner-join `Places.Id` to `Submissions.location_id`.
5. Count matching Submissions per Place.
6. Export rows with a count of one separately from rows with a count above one.

**Places without Submissions and Submissions without a matching Place are
excluded from both output files.** Their counts appear in the processing summary.
This preserves the original notebook's inner join.

For example, if Place 1 has Submission 10 and Place 2 has Submissions 11 and 12,
the single output has one row and the multiple output has two rows. Place 3 with
no Submissions is excluded. A Submission referring to Place 99 is excluded if
Place 99 is absent from the input. Multiple rows are retained; no automatic
choice of a preferred or latest response is made.

## Requirements and installation

Use Python 3.9 or later with a compatible version of pandas. Only pandas is
required for the script and regression tests. JupyterLab and ipykernel are
additional requirements for opening and running the notebook interactively.

From the repository root, using an existing environment or creating one:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pandas
```

For notebooks:

```bash
python -m pip install jupyterlab ipykernel
python -m ipykernel install --user --name cartospot-data-scripts --display-name "CartoSpot data scripts"
python -m jupyterlab
```

Select the **CartoSpot data scripts** kernel when opening the notebook. The
notebook no longer runs installation commands or installs the unused `scdata`
package. Dependencies should be installed in the selected kernel environment.

## Input schema

Inputs must be comma-separated UTF-8 CSVs with a header. A UTF-8 BOM is accepted.
Headers are case-sensitive. Supply a matching pair from the same dataset/export;
the workflow cannot prove that two independently supplied files belong together.

| File | Required columns | Treatment |
| --- | --- | --- |
| Places | `Id`, `location_type`, `tree_height`, `geometry_x`, `geometry_y`, `dataset_name`, `url` | These seven columns are retained; other Place columns are omitted. |
| Submissions | `Id`, `location_id` | All Submission columns, including custom responses, are retained. |

Values are loaded as text, so large IDs and literal responses such as `NA` are
preserved. Blank attribute values are allowed. IDs must be nonblank positive
integers; surrounding spaces, leading zeros and an all-zero decimal suffix
are normalized (`"0012.0"` becomes `"12"`). Fractional, negative and zero IDs
are rejected. Normalization happens before duplicate checking.

Coordinates and tree measurements are copied as supplied. **This workflow does
not validate geographic ranges, units, species, or tree health.** Perform such
checks separately before using the outputs as validated survey data.

Duplicate headers, duplicate Place IDs, duplicate Submission IDs, missing
required columns and ambiguous merge column names stop processing. A source
Submission column named `submission_count` is rejected because that name is
reserved for the calculated result. These checks avoid silently inflated counts
or overwritten fields. No automatic deduplication is performed.

## Run the script

Default input paths are resolved relative to the project root using
`project_paths.py`, independently of the terminal's current directory:

- `outputs/csv_files/places.csv`
- `outputs/csv_files/submissions.csv`

From the repository root:

```bash
python src/INTERVAL_process_data.py
```

Use explicit paths for a particular dataset or export:

```bash
python src/INTERVAL_process_data.py \
  --places "/path/to/dataset/places.csv" \
  --submissions "/path/to/dataset/submissions.csv" \
  --output-dir "outputs/interval_local/my_dataset"
```

| Argument | Default |
| --- | --- |
| `--places PATH` | `<project-root>/outputs/csv_files/places.csv` |
| `--submissions PATH` | `<project-root>/outputs/csv_files/submissions.csv` |
| `--output-dir PATH` | `<project-root>/outputs/csv_files/` |
| `--help` | Print command-line help and exit. |

Explicit relative paths are resolved against your terminal's current directory.
The script prints a JSON summary followed by the output paths. Exit status `0`
means processing and export completed, `1` means an input/processing/output error,
and `2` is used by argparse for invalid command-line arguments.

## Run the notebook

1. Start Jupyter from the repository root or `src/`.
2. Open `src/INTERVAL_process_data.ipynb`.
3. Set `INPUT_DIR` and `OUTPUT_DIR` in the path configuration cell. Alternatively,
   set `PLACES_PATH` and `SUBMISSIONS_PATH` individually.
4. Restart the kernel and run all cells in order.
5. Inspect the source previews, summary and output previews before using the files.

The notebook imports `read_csv`, `process_data` and `write_outputs` from its
matching Python module. Changes to the processing logic therefore apply to both
interfaces. Restart the kernel after editing the module to avoid stale imports.
Fix any raised input error before continuing with later cells.

## Output files and columns

| File | Contents |
| --- | --- |
| `places_with_single_submission.csv` | One row for each Place with exactly one matching Submission. |
| `places_with_multiple_submissions.csv` | All matching Submission rows for Places with two or more Submissions. |

The output folder is created when needed. Both CSVs are UTF-8 with headers and
no DataFrame index. Empty groups still produce header-only CSVs. Existing files
with these names are overwritten on rerun, so choose separate directories for
datasets or snapshots. Output paths are checked against the supplied input paths
to prevent overwriting a source CSV. The pair is not written as a transaction;
an I/O failure can leave a partially updated pair, so rerun after fixing it.

Column naming preserves the original pandas merge convention:

- `Id_x`: Place ID.
- `Id_y`: Submission ID.
- `location_id`: the Submission's reference to the Place.
- `submission_count`: number of matching Submissions for the Place, repeated
  on every output row belonging to that Place.
- Other names appearing in both tables gain `_x` for the Place value and `_y`
  for the Submission value, for example `url_x` and `url_y`.
- Non-overlapping names retain their original spelling.

The summary distinguishes **Places** from **Submission rows**. In particular,
`multiple_submission_places` counts unique Places, while
`multiple_submission_rows` counts all their matching Submissions.
`places_without_submissions` and `unmatched_submissions` explain excluded records.
The summary is printed by the script and available as `result.summary` in Python;
it is not saved as a third output file.

## Synthetic demonstration

The repository includes a small, entirely synthetic input pair under
`tests/fixtures/interval_local/`. It contains three Places and four Submissions,
including one unmatched Submission. It does not contain real participant data.

```bash
python src/INTERVAL_process_data.py \
  --places tests/fixtures/interval_local/places.csv \
  --submissions tests/fixtures/interval_local/submissions.csv \
  --output-dir outputs/interval_local_demo
```

Expected: one single-submission Place, one multiple-submission Place represented
by two rows, one Place without Submissions, and one unmatched Submission.

## Tests and verification

From the repository root:

```bash
python -m unittest discover -s tests -v
```

The 12 tests cover valid-input parity with the original merge, the split and
exclusion counts, duplicate IDs, missing columns, invalid and large IDs, empty
inputs, ambiguous column names, file errors, source overwrite protection,
command-line execution, and notebook/script output parity. The notebook test
executes every Python cell sequentially with synthetic paths from both supported
working directories. It does not test the Jupyter front end or kernel integration.

Verified in the development workspace with Python 3.12.14 and pandas 2.2.3.
Real exported CSVs and an interactive Jupyter kernel run still need validation.

## Troubleshooting

- **File not found:** check the input paths. This workflow requires existing raw
  CSV files; it does not call the API. The v3 exporter's Place summary CSV has a
  different schema and is not a direct substitute for `places.csv` here.
- **Missing required columns:** compare the headers with the input schema above.
- **Duplicate IDs:** inspect the source export for repeated records or mixed
  datasets. Decide which records to retain before rerunning.
- **No matching rows:** compare `Places.Id` with `Submissions.location_id` and
  confirm that the files belong together. Header-only results are valid.
- **No module named pandas:** install pandas in the same Python environment or
  notebook kernel that executes the workflow.
- **Unexpected row counts:** the multiple output contains one row per Submission,
  so its row count is greater than its number of unique Places.
