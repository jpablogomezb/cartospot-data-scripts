#!/usr/bin/env python3
"""Download an INTERVAL CSV ZIP, split submissions, and export GIS files."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from INTERVAL_process_data import normalize_ids, read_csv, require_columns
from project_paths import project_root

API_TEMPLATE = 'https://api.cartospot.com/api/v2/db/cartospot/generate-csv/interval/{dataset_id}/'
MAX_BYTES = 100 * 1024 * 1024
PLACE_COLUMNS = ['Id', 'location_type', 'tree_privacy', 'NTM_ID', 'tree_height',
                 'avg_tree_height', 'canopy_perimeter', 'canopy_area', 'geometry_x',
                 'geometry_y', 'dataset_id', 'dataset_name', 'url']
SUBMISSION_COLUMNS = ['Id', 'location_id', 'does_a_tree_exist_here', 'species',
                      'tree_type', 'health', 'circumference', 'dbh', 'notes',
                      'image_url', 'submitter_name']
SHP_NAMES = {'location_id': 'loc_id', 'location_type': 'loc_type',
             'tree_privacy': 'privacy', 'tree_height': 'height',
             'avg_tree_height': 'avg_height', 'canopy_perimeter': 'canopy_per',
             'canopy_area': 'canopy_are', 'dataset_name': 'dataset',
             'location_url': 'loc_url', 'submission_id': 'sub_id',
             'does_a_tree_exist_here': 'tree_exist', 'circumference': 'circumf',
             'submitter_name': 'submitter'}


@dataclass
class SplitResult:
    zero_or_one: pd.DataFrame
    multiple: pd.DataFrame
    summary: dict


def positive_id(value) -> int:
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError('Dataset ID must be a positive integer')
    return int(text)


def download_zip(dataset_id: int) -> bytes:
    """Fetch the fixed CartoSpot endpoint with retries, timeouts and a size limit."""
    url = API_TEMPLATE.format(dataset_id=positive_id(dataset_id))
    with requests.Session() as session:
        retries = Retry(total=3, backoff_factor=0.5,
                        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
        session.mount('https://', HTTPAdapter(max_retries=retries))
        with session.get(url, timeout=(10, 60), stream=True) as response:
            response.raise_for_status()
            buffer = io.BytesIO()
            for chunk in response.iter_content(1024 * 1024):
                if buffer.tell() + len(chunk) > MAX_BYTES:
                    raise ValueError('Downloaded ZIP exceeds the 100 MiB limit')
                buffer.write(chunk)
            return buffer.getvalue()


def unpack_inputs(data: bytes, destination: Path) -> tuple[Path, Path]:
    """Read exactly one places.csv and submissions.csv; never extract arbitrary paths."""
    if len(data) > MAX_BYTES:
        raise ValueError('Source ZIP exceeds the 100 MiB limit')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if sum(info.file_size for info in members) > MAX_BYTES:
            raise ValueError('Uncompressed ZIP exceeds the 100 MiB limit')
        selected = {}
        for info in members:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename:
                raise ValueError('ZIP contains an unsafe path')
            if path.name in ('places.csv', 'submissions.csv') and not info.is_dir():
                if path.name in selected:
                    raise ValueError(f'ZIP contains more than one {path.name}')
                selected[path.name] = info
        if set(selected) != {'places.csv', 'submissions.csv'}:
            raise ValueError('ZIP must contain exactly one places.csv and one submissions.csv')
        # Read both successfully before publishing either; old files are never inputs.
        contents = {name: archive.read(info) for name, info in selected.items()}
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    return destination / 'places.csv', destination / 'submissions.csv'


def process_data(places: pd.DataFrame, submissions: pd.DataFrame, dataset_id: int) -> SplitResult:
    dataset_id = positive_id(dataset_id)
    require_columns(places, PLACE_COLUMNS, 'Places')
    require_columns(submissions, SUBMISSION_COLUMNS + ['other_species'], 'Submissions')
    left = places[PLACE_COLUMNS].copy()
    right = submissions[SUBMISSION_COLUMNS + ['other_species']].copy()
    for frame, label in ((left, 'Places'), (right, 'Submissions')):
        frame['Id'] = normalize_ids(frame['Id'], f'{label}.Id')
        if frame['Id'].duplicated().any():
            raise ValueError(f'{label}: duplicate Id values')
    right['location_id'] = normalize_ids(right['location_id'], 'Submissions.location_id')
    dataset_ids = normalize_ids(left['dataset_id'], 'Places.dataset_id')
    if not dataset_ids.eq(str(dataset_id)).all():
        raise ValueError('Places.dataset_id does not match the requested dataset')
    other = right['other_species'].astype('string').str.strip()
    replace = right['species'].eq('Other') & other.notna() & other.ne('')
    right.loc[replace, 'species'] = other.loc[replace]
    left = left.rename(columns={'Id': 'location_id', 'url': 'location_url'})
    right = right.drop(columns='other_species').rename(columns={'Id': 'submission_id'})
    merged = left.merge(right, on='location_id', how='left', sort=False, validate='one_to_many')
    counts = merged.groupby('location_id')['submission_id'].transform('count')
    multiple = merged.loc[counts.gt(1)].copy()
    zero_or_one = merged.loc[counts.le(1)].copy()
    return SplitResult(zero_or_one, multiple, {
        'dataset_id': dataset_id, 'input_places': len(left), 'input_submissions': len(right),
        'zero_or_one_places': len(zero_or_one),
        'multiple_submission_places': int(multiple['location_id'].nunique()),
        'multiple_submission_rows': len(multiple),
        'places_without_submissions': int(merged['submission_id'].isna().sum()),
        'unmatched_submissions': int((~right['location_id'].isin(left['location_id'])).sum()),
    })


def scalar(value):
    """Represent missing attributes as JSON null; preserve source text otherwise."""
    return None if pd.isna(value) or value == '' else value


def make_features(frame: pd.DataFrame) -> tuple[list, list]:
    features, invalid = [], []
    for record in frame.to_dict(orient='records'):
        location_id = str(record.pop('location_id'))
        x, y = record.pop('geometry_x'), record.pop('geometry_y')
        try:
            lon, lat = float(x), float(y)
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError('Invalid coordinates')
            geometry = {'type': 'Point', 'coordinates': [lon, lat]}
        except (TypeError, ValueError):
            geometry = None
            invalid.append(location_id)
        features.append({'type': 'Feature', 'id': location_id, 'geometry': geometry,
                         'properties': {key: scalar(value) for key, value in record.items()}})
    return features, invalid


def write_shapefile(features: list, columns: list, path: Path) -> list[Path]:
    # Explicit schema handles empty datasets and null geometries consistently.
    import fiona
    from fiona.crs import CRS

    columns = [name for name in columns if name not in ('geometry_x', 'geometry_y')]
    names = {name: SHP_NAMES.get(name, name) for name in columns}
    schema = {'geometry': 'Point', 'properties': {names[name]: 'str:254' for name in columns}}
    rows = []
    for feature in features:
        values = {'location_id': feature['id'], **feature['properties']}
        properties = {}
        for name in columns:
            value = values[name]
            value = None if value is None else str(value)
            if value is not None and len(value.encode('utf-8')) > 254:
                raise ValueError(f'Shapefile field {name} exceeds 254 UTF-8 bytes at Place {feature["id"]}; use --skip-shapefile')
            properties[names[name]] = value
        rows.append({'geometry': feature['geometry'], 'properties': properties})
    with fiona.open(path, 'w', driver='ESRI Shapefile', crs=CRS.from_epsg(4326),
                    schema=schema, encoding='UTF-8') as sink:
        sink.writerecords(rows)
    return [path.with_suffix(ext) for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')]


def run_export(dataset_id: int, output_dir: Path | None = None,
               source_zip: Path | None = None, skip_shapefile: bool = False) -> Path:
    """Return a completed run directory; failed runs are removed."""
    dataset_id = positive_id(dataset_id)
    if source_zip is None:
        data = download_zip(dataset_id)
    else:
        with Path(source_zip).open('rb') as source:
            data = source.read(MAX_BYTES + 1)
    base = Path(output_dir) if output_dir is not None else project_root() / 'outputs' / 'interval_api_split'
    parent = base / f'dataset_id_{dataset_id}'
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.pending_', dir=parent))
    try:
        places_path, submissions_path = unpack_inputs(data, stage / 'csv_files')
        result = process_data(read_csv(places_path), read_csv(submissions_path), dataset_id)
        stem = f'data_valid_submissions_dataset_id_{dataset_id}'
        valid_csv = stage / 'csv_files' / f'{stem}.csv'
        multiple_csv = stage / 'csv_files' / f'data_multiple_submissions_dataset_id_{dataset_id}.csv'
        result.zero_or_one.to_csv(valid_csv, index=False, encoding='utf-8')
        result.multiple.to_csv(multiple_csv, index=False, encoding='utf-8')
        geodir = stage / 'geofiles'
        geodir.mkdir()
        features, invalid = make_features(result.zero_or_one)
        geojson_path = geodir / f'{stem}.geojson'
        geojson_path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features},
                                         ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
        files = [places_path, submissions_path, valid_csv, multiple_csv, geojson_path]
        if not skip_shapefile:
            files.extend(write_shapefile(features, list(result.zero_or_one.columns), geodir / f'{stem}.shp'))
        report = {**result.summary, 'invalid_geometry_place_ids': invalid,
                  'shapefile_written': not skip_shapefile,
                  'source': str(Path(source_zip).resolve()) if source_zip else API_TEMPLATE.format(dataset_id=dataset_id),
                  'created_utc': datetime.now(timezone.utc).isoformat(),
                  'shapefile_fields': SHP_NAMES if not skip_shapefile else {}}
        report_path = stage / 'processing_report.json'
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        files.append(report_path)
        with zipfile.ZipFile(stage / f'dataset_id_{dataset_id}_outputs.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.name)
        completed = parent / (datetime.now(timezone.utc).strftime('run_%Y%m%dT%H%M%S_') + stage.name.removeprefix('.pending_'))
        stage.rename(completed)
        return completed
    except Exception:
        shutil.rmtree(stage)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-id', required=True, type=positive_id)
    parser.add_argument('--source-zip', type=Path, help='Read a previously downloaded ZIP instead of calling the API')
    parser.add_argument('--output-dir', type=Path, help='Root for isolated dataset/run folders')
    parser.add_argument('--skip-shapefile', action='store_true', help='Export CSV and GeoJSON without Shapefile limits')
    args = parser.parse_args(argv)
    try:
        run = run_export(args.dataset_id, args.output_dir, args.source_zip, args.skip_shapefile)
    except (OSError, ValueError, requests.RequestException, zipfile.BadZipFile, ImportError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    print((run / 'processing_report.json').read_text())
    print(f'Export completed: {run}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
