"""Optional real-kernel notebook smoke test: install nbclient and ipykernel."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_interval_api_split import ROOT, archive_bytes
import INTERVAL_process_data_from_API_and_generated_files as api

AVAILABLE = all(importlib.util.find_spec(name) for name in ('nbclient', 'ipykernel'))


@unittest.skipUnless(AVAILABLE, 'Install nbclient and ipykernel for real-kernel tests')
class NotebookTests(unittest.TestCase):
    def test_notebook_kernel_parity(self):
        import nbformat
        from nbclient import NotebookClient
        from jupyter_client import KernelManager
        from jupyter_client.kernelspec import KernelSpecManager

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'input.zip'
            source.write_bytes(archive_bytes())
            expected = api.run_export(306, folder / 'expected', source)
            kernel_dir = folder / 'kernels' / 'interval-test'
            kernel_dir.mkdir(parents=True)
            (kernel_dir / 'kernel.json').write_text(json.dumps({
                'argv': [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}'],
                'display_name': 'INTERVAL test', 'language': 'python'}))
            for index, cwd in enumerate((ROOT, ROOT / 'src')):
                notebook = nbformat.read(ROOT / 'src' / 'INTERVAL_process_data_from_API_and_generated_files.ipynb', as_version=4)
                output = folder / f'notebook_{index}'
                for cell in notebook.cells:
                    if cell.cell_type == 'code' and 'DATASET_ID = 306' in cell.source:
                        cell.source = f'DATASET_ID = 306\nSOURCE_ZIP = Path({str(source)!r})\nOUTPUT_DIR = Path({str(output)!r})\nSKIP_SHAPEFILE = False'
                manager = KernelManager(kernel_name='interval-test',
                    kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernel_dir.parent)]))
                try:
                    NotebookClient(notebook, km=manager, timeout=60, resources={'metadata': {'path': str(cwd)}}).execute()
                finally:
                    if manager.has_kernel:
                        manager.shutdown_kernel(now=True)
                run = next((output / 'dataset_id_306').glob('run_*'))
                for path in (expected / 'csv_files').glob('*.csv'):
                    self.assertEqual(path.read_bytes(), (run / 'csv_files' / path.name).read_bytes())
                expected_geo = next((expected / 'geofiles').glob('*.geojson'))
                self.assertEqual(expected_geo.read_bytes(), (run / 'geofiles' / expected_geo.name).read_bytes())


if __name__ == '__main__':
    unittest.main()
