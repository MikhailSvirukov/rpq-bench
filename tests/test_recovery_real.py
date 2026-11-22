"""Integration checks against the compiled C driver (when available)."""
import csv
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'build/rpq_paths_benchmark'


@unittest.skipUnless(sys.platform.startswith('linux') and DRIVER.is_file(),
                     'requires Linux and a built C driver')
class RealRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queries = self.root / 'queries'
        self.matrix = self.root / '7.txt'
        self.matrix.write_text('%%MatrixMarket matrix coordinate pattern general\n3 3 1\n1 2\n')
        for number in range(1, 4):
            query = self.queries / str(number)
            query.mkdir(parents=True)
            (query / 'meta.txt').write_text('1 0\n1 1\n1 2\n1 7\n')
            (query / '7.txt').write_text('%%MatrixMarket matrix coordinate pattern general\n2 2 1\n1 2\n')

    def test_resume_skips_completed_calls(self):
        journal = self.root / 'journal.tsv'
        result = subprocess.run([str(DRIVER), '--invocation-log', str(journal),
            '--resume-at', '3', 'trails', '1', '2', '1', '7', str(self.matrix),
            *(str(self.queries / str(n)) for n in range(1,4))],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([int(line.split('\t')[1]) for line in journal.read_text().splitlines()],
                         [i for i in range(3,9) for _ in range(2)])
        self.assertEqual(len(result.stdout.splitlines()), 7)

    def test_real_driver_killed_during_query(self):
        meta = self.queries / '2/7.txt'
        meta.unlink()
        os.mkfifo(meta)
        output = self.root / 'out'
        wrapper_build = self.root / 'wrapper-build'
        wrapper_build.mkdir()
        pid_file = self.root / 'driver.pid'
        wrapper = wrapper_build / 'rpq_paths_benchmark'
        wrapper.write_text(f'#!{sys.executable}\nimport os, sys\n'
            f'from pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\n'
            f'os.execv({str(DRIVER)!r}, [{str(DRIVER)!r}, *sys.argv[1:]])\n')
        wrapper.chmod(0o755)
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/rpq_paths_bench.py'),
            str(self.queries), str(self.matrix), '--semantics', 'trails',
            '--build-dir', str(wrapper_build), '--output', str(output)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer = None
        try:
            deadline = time.monotonic() + 10
            while writer is None:
                try:
                    writer = os.open(meta, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO: raise
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline, 'driver did not reach FIFO')
                    time.sleep(0.01)
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
            os.close(writer)
            writer = None
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 137, stderr)
            self.assertEqual([line.split('\t')[0] for line in stdout.splitlines()[1:]], ['1','3'])
            with (output / 'failures.tsv').open() as stream:
                rows = list(csv.DictReader(stream, delimiter='\t'))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['query'], '2')
            metadata = json.loads((output / 'run.json').read_text())
            self.assertEqual([a['resume_at'] for a in metadata['attempts']], [0,2])
        finally:
            if writer is not None: os.close(writer)
            if process.poll() is None: process.kill()
            process.communicate()

    def test_api_error_continues_without_restart(self):
        (self.queries / '2/meta.txt').write_text('invalid metadata')
        output = self.root / 'out'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/rpq_paths_bench.py'),
            str(self.queries), str(self.matrix), '--iterations', '2', '--output', str(output)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([line.split('\t')[0] for line in result.stdout.splitlines()[1:]],
                         ['1','3','1','3'])
        with (output / 'failures.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertEqual([row['status'] for row in rows], ['algorithm_error'] * 2)
        self.assertEqual(len(json.loads((output / 'run.json').read_text())['attempts']), 1)
