"""Exercise the verification switch at the Python/C process boundary."""
from pathlib import Path
import shutil
import json
import os
import select
import time
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MATRIX = '%%MatrixMarket matrix coordinate pattern general\n3 3 1\n1 2\n'


class VerificationSwitchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queries = self.root / 'queries'
        query = self.queries / '1'
        query.mkdir(parents=True)
        self.metadata = query / 'meta.txt'
        self.metadata.write_text('1 0\n1 1\n1 2\n1 7\n')
        (query / '7.txt').write_text(MATRIX)
        self.matrices = self.root / 'matrices'
        self.matrices.mkdir()
        self.matrix = self.matrices / '7.txt'
        self.matrix.write_text(MATRIX)
        self.build = self.root / 'build'
        self.build.mkdir()
        self.marker = self.build / 'invoked'
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(f'''#!{sys.executable}
from pathlib import Path
import os
Path(__file__).with_name('invoked').write_text('yes')
samples = os.environ.get('RPQ_BENCH_VERIFY_SAMPLES')
if samples:
    Path(samples).write_text('1\\t1\\n1\\t2\\n')
print('query\\ttime_seconds\\tpaths\\n1\\t0.1\\t2')
''')
        driver.chmod(0o755)

    def launch(self, verify):
        self.marker.unlink(missing_ok=True)
        command = [sys.executable, str(ROOT / 'scripts/rpq_paths_bench.py'),
                   str(self.queries), str(self.matrices), '--build-dir', str(self.build)]
        if verify:
            command.append('--verify')
        return subprocess.run(command, capture_output=True, text=True)

    def test_metadata_verification_is_opt_in(self):
        self.metadata.write_text('not metadata')
        self.assertEqual(self.launch(False).returncode, 0)
        self.assertTrue(self.marker.exists())
        self.assertNotEqual(self.launch(True).returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_matrix_verification_is_opt_in(self):
        self.matrix.write_text(MATRIX.replace('1 2\n', '0 2\n'))
        self.assertEqual(self.launch(False).returncode, 0)
        self.assertTrue(self.marker.exists())
        self.assertNotEqual(self.launch(True).returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_count_verification_is_opt_in(self):
        self.assertEqual(self.launch(False).returncode, 0)
        result = self.launch(True)
        self.assertTrue(self.marker.exists())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('path count changed', result.stderr)

    def test_any_any_queries_are_silently_filtered_before_driver_and_verification(self):
        for number, endpoints in ((2, '0 3'), (3, '0 0'), (4, '1 3')):
            query = self.queries / str(number)
            query.mkdir()
            (query / 'meta.txt').write_text(f'{endpoints}\n1 1\n1 2\n1 7\n')
            if number != 3:  # Skipped queries need no NFA files, even with --verify.
                (query / '7.txt').write_text(MATRIX)
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(f'''#!{sys.executable}
from pathlib import Path
import os, sys
samples = os.environ.get('RPQ_BENCH_VERIFY_SAMPLES')
if samples:
    Path(samples).write_text('')
print('query\\ttime_seconds\\tpaths', flush=True)
for phase, passes in [('warmup', int(sys.argv[2])), ('measured', int(sys.argv[3]))]:
    for iteration in range(1, passes + 1):
        for query in sys.argv[5 + 2 * int(sys.argv[4]):]:
            name = Path(query).name
            if os.environ.get('RPQ_BENCH_PROCESS'):
                print(f'query {{name}}: {{phase}} pass {{iteration}}', file=sys.stderr, flush=True)
                print(f'[process] query {{query}}: running algorithm', file=sys.stderr, flush=True)
                print(f'[process] query {{query}}: algorithm returned (info=0)', file=sys.stderr, flush=True)
            if phase == 'measured':
                print(f'{{name}}\\t0.1\\t1', flush=True)
                if samples:
                    with open(samples, 'a') as stream:
                        stream.write(f'{{name}}\\t1\\n')
''')
        for verify in (False, True):
            if verify:
                # Existing verification still rejects two fixed endpoints.
                shutil.rmtree(self.queries / '4')
            for memory in (False, True):
                if memory and not sys.platform.startswith('linux'):
                    continue
                output = self.root / f'filtered-{verify}-{memory}'
                command = self.output_command(output) + ['--process', '--warmup', '1', '--iterations', '2']
                if verify:
                    command.append('--verify')
                if memory:
                    command.append('--memory')
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ['1', '2'] if verify else ['1', '2', '4']
                self.assertEqual([line.split('\t')[0] for line in result.stdout.splitlines()[1:]], expected * 2)
                self.assertNotIn('query 3:', result.stderr)
                self.assertNotIn(str(self.queries / '3'), result.stderr)
                metadata = json.loads((output / 'run.json').read_text())
                self.assertEqual(metadata['command'][-len(expected):], [str(self.queries / name) for name in expected])
                for path in output.iterdir():
                    self.assertNotIn(str(self.queries / '3'), path.read_text())
                if memory:
                    import csv
                    with (output / 'memory.tsv').open() as stream:
                        self.assertEqual([row['query'] for row in csv.DictReader(stream, delimiter='\t')], expected * 3)

    def test_all_any_any_queries_exit_silently_without_files_or_driver(self):
        self.metadata.write_text('\n  00\t\n0\n')
        (self.build / 'rpq_paths_benchmark').unlink()
        for single in (False, True):
            for existing_output in (False, True):
                output = self.root / f'empty-{single}-{existing_output}'
                if existing_output:
                    output.mkdir()
                    (output / 'results.tsv').write_text('previous results\n')
                command = self.output_command(output) + ['--verify', '--process']
                if single:
                    command[2] = str(self.metadata.parent)
                if sys.platform.startswith('linux'):
                    command.append('--memory')
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, '')
                self.assertFalse(self.marker.exists())
                if existing_output:
                    self.assertEqual(list(output.iterdir()), [output / 'results.tsv'])
                    self.assertEqual((output / 'results.tsv').read_text(), 'previous results\n')
                else:
                    self.assertFalse(output.exists())

    def output_command(self, output):
        return [sys.executable, str(ROOT / 'scripts/rpq_paths_bench.py'),
                str(self.queries), str(self.matrices), '--build-dir', str(self.build),
                '--output', str(output)]

    def test_output_duplicates_stdout(self):
        baseline = self.launch(False)
        output = self.root / 'results'
        result = subprocess.run(self.output_command(output), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, baseline.stdout)
        self.assertEqual((output / 'results.tsv').read_text(), result.stdout)

    def test_output_preserves_failure_and_completed_rows(self):
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(driver.read_text() + "\nimport sys\nprint('query failed', file=sys.stderr)\nsys.exit(7)\n")
        output = self.root / 'results'
        result = subprocess.run(self.output_command(output), capture_output=True, text=True)
        self.assertEqual(result.returncode, 7)
        self.assertIn('1\t0.1\t2', result.stdout)
        self.assertEqual((output / 'results.tsv').read_text(), result.stdout)
        self.assertIn('query failed', (output / 'errors.log').read_text())
        self.assertEqual(json.loads((output / 'run.json').read_text())['exit_code'], 7)

    def test_output_is_visible_before_driver_finishes(self):
        fifo = self.root / 'continue'
        os.mkfifo(fifo)
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(f"""#!{sys.executable}
import sys
print('query\\ttime_seconds\\tpaths', flush=True)
print('1\\t0.1\\t2', flush=True)
with open({str(fifo)!r}) as stream:
    stream.read()
print('2\\t0.2\\t3', flush=True)
""")
        output = self.root / 'results'
        process = subprocess.Popen(self.output_command(output), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        received = b''
        try:
            deadline = time.monotonic() + 10
            while received.count(b'\n') < 2:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, 'stdout was buffered until process exit')
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                self.assertTrue(ready, 'stdout was buffered until process exit')
                chunk = os.read(process.stdout.fileno(), 4096)
                self.assertTrue(chunk, 'launcher exited before publishing results')
                received += chunk
            self.assertIsNone(process.poll())
            self.assertEqual((output / 'results.tsv').read_bytes(), received)
            # The reader opens the FIFO after publishing its first rows.
            while True:
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    os.close(fd)
                    break
                except OSError:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            tail, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual((output / 'results.tsv').read_bytes(), received + tail)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_process_progress_is_opt_in(self):
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(driver.read_text() + "\nimport sys\nif os.environ.get('RPQ_BENCH_PROCESS'):\n    print('[process] loading matrix', file=sys.stderr, flush=True)\n")
        for enabled in (False, True):
            for save in (False, True):
                output = self.root / f'process-{enabled}-{save}'
                command = self.output_command(output)
                if not save:
                    command = command[:-2]
                if enabled:
                    command.append('--process')
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('[process] loading matrix' in result.stderr, enabled)
                self.assertNotIn('[process]', result.stdout)
                if save:
                    self.assertEqual((output / 'errors.log').read_text(), result.stderr)
                    self.assertEqual(json.loads((output / 'run.json').read_text())['process'], enabled)

    def test_process_is_visible_before_any_stdout(self):
        fifo = self.root / 'continue-progress'
        os.mkfifo(fifo)
        # Opening both ends keeps cleanup nonblocking even if the child fails.
        gate = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, gate)
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(f"""#!{sys.executable}
import sys
print('[process] reading matrix', file=sys.stderr, flush=True)
with open({str(fifo)!r}) as stream:
    stream.read(1)
print('query\\ttime_seconds\\tpaths', flush=True)
""")
        output = self.root / 'progress-live'
        process = subprocess.Popen(self.output_command(output) + ['--process'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready, _, _ = select.select([process.stderr], [], [], 10)
            self.assertTrue(ready, 'progress waits for stdout or process exit')
            received = os.read(process.stderr.fileno(), 4096)
            self.assertEqual(received, b'[process] reading matrix\n')
            self.assertIsNone(process.poll())
            self.assertEqual((output / 'results.tsv').read_bytes(), b'')
            self.assertEqual((output / 'errors.log').read_bytes(), received)
            os.write(gate, b'x')
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, b'query\ttime_seconds\tpaths\n')
        finally:
            if process.poll() is None:
                os.write(gate, b'x')
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_download_verification_is_opt_in(self):
        self.metadata.write_text('not metadata')
        queries = shutil.make_archive(str(self.root / 'q'), 'zip', self.queries)
        matrices = shutil.make_archive(str(self.root / 'm'), 'zip', self.matrices)
        for verify in (False, True):
            output = self.root / f'download-{verify}'
            command = [sys.executable, str(ROOT / 'scripts/download_data.py'),
                       '--queries-url', Path(queries).as_uri(),
                       '--matrices-url', Path(matrices).as_uri(), '--output', str(output)]
            if verify:
                command.append('--verify')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, not verify, result.stderr)
            self.assertEqual(output.exists(), not verify)


if __name__ == '__main__':
    unittest.main()
