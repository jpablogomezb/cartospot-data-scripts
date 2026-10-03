"""Synthetic ZIP, HTTP and GIS regression tests; no live API calls."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import fiona
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import INTERVAL_process_data_from_API_and_generated_files as api
from INTERVAL_process_data import read_csv
FIXTURES = ROOT / 'tests' / 'fixtures' / 'interval_api_split'


def archive_bytes(names=None):
    buffer = io.BytesIO()
    if names is None:
        names = {name: (FIXTURES / name).read_bytes() for name in ('places.csv', 'submissions.csv')}
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in names.items(): archive.writestr(name, data)
    return buffer.getvalue()


class ApiSplitTests(unittest.TestCase):
    def setUp(self):
        self.places = read_csv(FIXTURES / 'places.csv')
        self.submissions = read_csv(FIXTURES / 'submissions.csv')

    def test_split_preserves_zero_and_multiple(self):
        result = api.process_data(self.places, self.submissions, 306)
        self.assertEqual(result.zero_or_one.location_id.tolist(), ['1', '3', '4'])
        self.assertEqual(result.multiple.submission_id.tolist(), ['11', '12'])
        self.assertEqual(result.summary['unmatched_submissions'], 1)
        self.assertEqual(result.summary['places_without_submissions'], 2)
        self.assertEqual(result.summary['multiple_submission_places'], 1)
        self.assertEqual(result.zero_or_one.iloc[0].species, 'Oak')
        self.assertEqual(result.multiple.iloc[1].species, 'Other')

    def test_legacy_group_parity(self):
        left = self.places.rename(columns={'Id': 'location_id', 'url': 'location_url'})
        right = self.submissions[api.SUBMISSION_COLUMNS].rename(columns={'Id': 'submission_id'})
        merged = left.merge(right, how='left', on='location_id')
        mask = merged.duplicated('location_id', keep=False)
        result = api.process_data(self.places, self.submissions, 306)
        self.assertEqual(merged.loc[~mask, 'location_id'].tolist(), result.zero_or_one.location_id.tolist())
        self.assertEqual(merged.loc[mask, 'submission_id'].tolist(), result.multiple.submission_id.tolist())

    def test_input_validation(self):
        for p, s in [(pd.concat([self.places, self.places.iloc[[0]]]), self.submissions),
                     (self.places, pd.concat([self.submissions, self.submissions.iloc[[0]]])),
                     (self.places.drop(columns='tree_privacy'), self.submissions),
                     (self.places, self.submissions.drop(columns='other_species')),
                     (self.places.assign(dataset_id='307'), self.submissions),
                     (self.places, self.submissions.assign(location_id=''))]:
            with self.assertRaises(ValueError): api.process_data(p, s, 306)
        for bad in (0, -1, 'abc', '1.2'):
            with self.assertRaises(ValueError): api.positive_id(bad)

    def test_geometry_nulls_and_order(self):
        for x, y in [('', '53'), ('bad', '53'), ('181', '53'), ('0', '91'), ('inf', '0')]:
            result = api.process_data(self.places.assign(geometry_x=x, geometry_y=y), self.submissions, 306)
            features, invalid = api.make_features(result.zero_or_one)
            self.assertEqual(len(invalid), 3)
            self.assertTrue(all(f['geometry'] is None for f in features))
            json.dumps(features, allow_nan=False)
        result = api.process_data(self.places, self.submissions, 306)
        features, invalid = api.make_features(result.zero_or_one)
        self.assertEqual(features[0]['geometry']['coordinates'], [-6.1, 53.1])
        self.assertEqual(invalid, ['4'])

    def test_zip_validation(self):
        cases = [b'not a zip', archive_bytes({'places.csv': b'Id\n1'}),
                 archive_bytes({'../places.csv': b'', 'submissions.csv': b''}),
                 archive_bytes({'a/places.csv': b'', 'b/places.csv': b'', 'submissions.csv': b''})]
        with tempfile.TemporaryDirectory() as tmp:
            for data in cases:
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    api.unpack_inputs(data, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
            with patch.object(api, 'MAX_BYTES', 5), self.assertRaises(ValueError):
                api.unpack_inputs(archive_bytes(), Path(tmp))

    def test_nested_zip_and_raw_bytes(self):
        data = archive_bytes({'export/' + name: (FIXTURES / name).read_bytes()
                              for name in ('places.csv', 'submissions.csv')})
        with tempfile.TemporaryDirectory() as tmp:
            paths = api.unpack_inputs(data, Path(tmp))
            for path in paths: self.assertEqual(path.read_bytes(), (FIXTURES / path.name).read_bytes())

    def test_http_timeout_errors_and_limit(self):
        session, response = MagicMock(), MagicMock()
        session.get.return_value.__enter__.return_value = response
        response.iter_content.return_value = [archive_bytes()]
        with patch.object(api.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            self.assertEqual(api.download_zip(306), archive_bytes())
            session.get.assert_called_once_with(api.API_TEMPLATE.format(dataset_id=306), timeout=(10, 60), stream=True)
            response.raise_for_status.side_effect = requests.HTTPError('404')
            with self.assertRaises(requests.HTTPError): api.download_zip(306)
            response.raise_for_status.side_effect = None
            session.get.side_effect = requests.Timeout('timeout')
            with self.assertRaises(requests.Timeout): api.download_zip(306)
            session.get.side_effect = None
            with patch.object(api, 'MAX_BYTES', 1), self.assertRaises(ValueError): api.download_zip(306)

    def test_full_export_roundtrip_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.zip'
            source.write_bytes(archive_bytes())
            runs = [api.run_export(306, Path(tmp) / 'out', source) for _ in range(2)]
            self.assertNotEqual(runs[0], runs[1])
            run = runs[0]
            report = json.loads((run / 'processing_report.json').read_text())
            self.assertEqual(report['invalid_geometry_place_ids'], ['4'])
            shape = next((run / 'geofiles').glob('*.shp'))
            with fiona.open(shape) as collection:
                self.assertEqual(len(collection), 3)
                self.assertEqual(collection.crs.to_epsg(), 4326)
                rows = list(collection)
                self.assertIsNone(rows[-1]['geometry'])
                self.assertEqual(rows[0]['properties']['notes'], 'Árbol, sano')
                self.assertEqual(rows[0]['properties']['loc_id'], '1')
            with zipfile.ZipFile(next(run.glob('*.zip'))) as archive:
                self.assertEqual(len(archive.namelist()), 11)
                for suffix in ('.shp', '.shx', '.dbf', '.prj', '.cpg'):
                    self.assertTrue(any(n.endswith(suffix) for n in archive.namelist()))
                self.assertEqual(archive.read('places.csv'), (FIXTURES / 'places.csv').read_bytes())

    def test_empty_and_all_multiple_exports(self):
        for p, s in [(self.places.iloc[:0], self.submissions.iloc[:0]),
                     (self.places.iloc[[1]], self.submissions.iloc[[1, 2]]),
                     (self.places, self.submissions.iloc[:0])]:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.zip'
                source.write_bytes(archive_bytes({'places.csv': p.to_csv(index=False), 'submissions.csv': s.to_csv(index=False)}))
                run = api.run_export(306, Path(tmp) / 'out', source)
                with fiona.open(next((run / 'geofiles').glob('*.shp'))) as collection:
                    self.assertEqual(len(collection), 4 if s.empty and not p.empty else 0)

    def test_shapefile_overflow_and_failed_run_cleanup(self):
        s = self.submissions.copy()
        s.loc[0, 'notes'] = 'é' * 128
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.zip'
            source.write_bytes(archive_bytes({'places.csv': self.places.to_csv(index=False), 'submissions.csv': s.to_csv(index=False)}))
            output = Path(tmp) / 'out'
            with self.assertRaisesRegex(ValueError, '254'): api.run_export(306, output, source)
            self.assertEqual(list((output / 'dataset_id_306').iterdir()), [])
            run = api.run_export(306, output, source, skip_shapefile=True)
            self.assertFalse(list((run / 'geofiles').glob('*.shp')))
            data = json.loads(next((run / 'geofiles').glob('*.geojson')).read_text())
            self.assertEqual(data['features'][0]['properties']['notes'], 'é' * 128)

    def test_cli_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.zip'
            source.write_bytes(archive_bytes())
            command = [sys.executable, str(ROOT / 'src' / 'INTERVAL_process_data_from_API_and_generated_files.py'),
                       '--dataset-id', '306', '--source-zip', str(source), '--output-dir', str(Path(tmp) / 'out')]
            done = subprocess.run(command, cwd=tmp, text=True, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            source.write_bytes(b'bad zip')
            failed = subprocess.run(command, cwd=tmp, text=True, capture_output=True)
            self.assertEqual(failed.returncode, 1)


if __name__ == '__main__':
    unittest.main()
