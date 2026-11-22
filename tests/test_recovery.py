"""Exercise recovery with real SIGKILL/SIGSEGV and the memory monitor."""
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = r"""#!PYTHON
import os, signal, sys, resource
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from pathlib import Path
options = {}
args = sys.argv[1:]
while args and args[0].startswith('--'):
    options[args[0]] = args[1]
    args = args[2:]
resume = int(options['--resume-at'])
with Path(__file__).with_name('loads').open('a') as log: log.write(str(resume) + '\n')
if os.environ.get('FAIL_LOADING'): os.kill(os.getpid(), signal.SIGKILL)
warmup, iterations, count = map(int, args[1:4])
queries = args[4 + 2 * count:]
journal = open(options['--invocation-log'], 'a', buffering=1)
samples = open(os.environ['RPQ_BENCH_VERIFY_SAMPLES'], 'a' if resume else 'w') if 'RPQ_BENCH_VERIFY_SAMPLES' in os.environ else None
if '--jit-error-log' in options:
    with open(options['--jit-error-log'], 'a' if resume else 'w') as log: log.write('attempt\n')
print('query\ttime_seconds\tpaths', flush=True)
for index in range(resume, len(queries) * (warmup + iterations)):
    phase = 'warmup' if index < warmup * len(queries) else 'measured'
    offset = 0 if phase == 'warmup' else warmup * len(queries)
    number = Path(queries[index % len(queries)]).name
    journal.write(f'begin\t{index}\t0\n')
    print(f'query {number}: {phase} pass {(index-offset)//len(queries)+1}', file=sys.stderr, flush=True)
    print(f'[process] query {queries[index % len(queries)]}: running algorithm', file=sys.stderr, flush=True)
    if index in set(map(int, os.environ.get('CRASH_AT', '1,5').split(','))):
        os.kill(os.getpid(), int(os.environ.get('CRASH_SIGNAL', signal.SIGKILL)))
    print(f'[process] query {queries[index % len(queries)]}: algorithm returned (info=0)', file=sys.stderr, flush=True)
    if phase == 'measured':
        print(f'{number}\t0.1\t2', flush=True)
        if samples: samples.write(f'{number}\t2\n'); samples.flush()
    journal.write(f'end\t{index}\t0\n')
"""


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queries = self.root / 'queries'
        for number in range(1, 4):
            query = self.queries / str(number)
            query.mkdir(parents=True)
            (query / 'meta.txt').write_text('1 0\n1 1\n1 2\n1 7\n')
            (query / '7.txt').write_text('%%MatrixMarket matrix coordinate pattern general\n2 2 1\n1 2\n')
        self.matrix = self.root / '7.txt'
        self.matrix.write_text('%%MatrixMarket matrix coordinate pattern general\n3 3 1\n1 2\n')
        self.build = self.root / 'build'
        self.build.mkdir()
        driver = self.build / 'rpq_paths_benchmark'
        driver.write_text(FAKE.replace('PYTHON', sys.executable, 1))
        driver.chmod(0o755)
        self.output = self.root / 'output'

    def launch(self, memory=False, output=True, extra_env=None):
        command = [sys.executable, str(ROOT / 'scripts/rpq_paths_bench.py'),
                   str(self.queries), str(self.matrix), '--build-dir', str(self.build),
                   '--warmup', '1', '--iterations', '2', '--verify']
        if output: command += ['--output', str(self.output)]
        if memory: command += ['--memory']
        return subprocess.run(command, capture_output=True, text=True,
                              env={**os.environ, **(extra_env or {})}, timeout=15)

    def test_kill_recovery_with_and_without_memory(self):
        for memory in (False, True):
            with self.subTest(memory=memory):
                result = self.launch(memory)
                self.assertEqual(result.returncode, 137, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(lines[0], 'query\ttime_seconds\tpaths')
                self.assertEqual([line.split('\t')[0] for line in lines[1:]], ['1','2','1','2','3'])
                self.assertEqual((self.output / 'results.tsv').read_text(), result.stdout)
                metadata = json.loads((self.output / 'run.json').read_text())
                self.assertEqual([a['resume_at'] for a in metadata['attempts']], [0,2,6])
                with (self.output / 'failures.tsv').open() as file:
                    failures = list(csv.DictReader(file, delimiter='\t'))
                self.assertEqual([f['query'] for f in failures], ['2','3'])
                self.assertEqual([f['phase'] for f in failures], ['warmup','measured'])
                self.assertTrue(all(f['status'] == 'sigkill' for f in failures))
                self.assertEqual(len((self.output / 'samples.tsv').read_text().splitlines()), 5)
                self.assertEqual((self.output / 'jit-errors.log').read_text(), 'attempt\n' * 3)
                if memory:
                    with (self.output / 'memory.tsv').open() as file:
                        rows = list(csv.DictReader(file, delimiter='\t'))
                    self.assertEqual(len(rows), 9)
                    self.assertEqual(sum(row['status'] == 'incomplete' for row in rows), 2)

    def test_recovery_without_output_directory(self):
        result = self.launch(output=False)
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 6)

    def test_loading_failure_is_not_retried(self):
        result = self.launch(extra_env={'FAIL_LOADING': '1'})
        self.assertEqual(result.returncode, 137)
        self.assertEqual((self.build / 'loads').read_text(), '0\n')

    def test_sigterm_is_not_retried(self):
        result = self.launch(extra_env={'CRASH_SIGNAL': str(signal.SIGTERM)})
        self.assertEqual(result.returncode, 143)
        self.assertEqual((self.build / 'loads').read_text(), '0\n')

    def test_segv_recovery(self):
        result = self.launch(extra_env={'CRASH_SIGNAL': str(signal.SIGSEGV)})
        self.assertEqual(result.returncode, 139)
        self.assertEqual(len(result.stdout.splitlines()), 6)

    def test_every_invocation_fails_without_looping(self):
        result = self.launch(extra_env={'CRASH_AT': ','.join(map(str, range(9)))})
        self.assertEqual(result.returncode, 137)
        self.assertEqual(len((self.build / 'loads').read_text().splitlines()), 9)
        self.assertEqual(result.stdout, 'query\ttime_seconds\tpaths\n')
