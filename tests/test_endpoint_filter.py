"""Selection of exactly one fixed endpoint, including direct driver calls."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('benchmark',ROOT/'scripts/rpq_paths_bench.py')
benchmark=importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

class EndpointFilterTest(unittest.TestCase):
    def test_only_valid_unsupported_pairs_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            for fields,expected in [
                ('0 0',True),('1 2',True),('001 002',True),
                ('1 0',False),('0 1',False),('00 003',False),('0\n1',False),
                ('0 9223372036854775807',False),('0 9223372036854775808',False),
                ('-1 2',False),('+1 2',False),('one two',False),('1',False),('',False),
            ]:
                with self.subTest(fields=fields):
                    (path/'meta.txt').write_text(fields)
                    self.assertEqual(benchmark.has_unsupported_endpoints(path),expected)
            (path/'meta.txt').unlink()
            self.assertFalse(benchmark.has_unsupported_endpoints(path))

    @unittest.skipUnless((ROOT/'build/rpq_paths_benchmark').is_file(),'requires built C driver')
    def test_direct_driver_rejects_unsupported_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            matrix=path/'7.txt'
            matrix.write_text('%%MatrixMarket matrix coordinate pattern general\n2 2 1\n1 2\n')
            for endpoints in ('0 0','1 2'):
                with self.subTest(endpoints=endpoints):
                    (path/'meta.txt').write_text(endpoints+'\n')
                    result=subprocess.run([str(ROOT/'build/rpq_paths_benchmark'),
                        'all','0','1','1','7',str(matrix),str(path)],
                        capture_output=True,text=True,timeout=10)
                    self.assertNotEqual(result.returncode,0)
                    self.assertIn('exactly one of source and destination must be fixed',result.stderr)
                    self.assertEqual(result.stdout,'query\ttime_seconds\tpaths\n')
