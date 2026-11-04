#!/usr/bin/env python3
"""Integration test using the compiled public API, no external datasets."""
import json
import csv
import math
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    matrices = base / 'matrices'
    queries = base / 'queries'
    matrices.mkdir()
    queries.mkdir()
    (matrices / '7.txt').write_text('%%MatrixMarket matrix coordinate pattern general\n3 3 2\n1 2\n2 3\n')
    for number, endpoints, label in [(1, '1 0', 7), (2, '0 3', 7), (3, '0 0', 7), (4, '3 0', -7)]:
        query = queries / str(number)
        query.mkdir()
        (query / 'meta.txt').write_text(f'{endpoints}\n1 1\n1 2\n1 {label}\n')
        (query / f'{label}.txt').write_text('%%MatrixMarket matrix coordinate pattern general\n2 2 1\n1 2\n')
    for verify in (False, True):
        for semantics in ('all', 'simple', 'trails', 'shortest'):
            output = base / f'{semantics}-{verify}'
            command = [sys.executable, str(root / 'scripts/rpq_paths_bench.py'), str(queries), str(matrices),
                       '--build-dir', str(root / 'build'), '--semantics', semantics,
                       '--warmup', '1', '--iterations', '2', '--output', str(output), '--stage-times']
            if verify:
                command.append('--verify')
            subprocess.run(command, check=True)
            lines = (output / 'results.tsv').read_text().splitlines()
            assert lines[0] == 'query\ttime_seconds\tpaths', lines
            assert [line.split('\t')[::2] for line in lines[1:]] == [['1', '1'], ['2', '1'], ['4', '1']] * 2, lines
            metadata = json.loads((output / 'run.json').read_text())
            assert metadata['exit_code'] == 0 and metadata['verify'] == verify
            assert (output / 'samples.tsv').exists() == verify
            with (output / 'stage_times.tsv').open() as stream:
                stage_rows = list(csv.DictReader(stream, delimiter='\t'))
            assert [row['query'] for row in stage_rows] == ['1', '2', '4'] * 3
            for row in stage_rows:
                assert all(math.isfinite(float(row[key])) and float(row[key]) >= 0
                           for key in ('matrix_seconds', 'reconstruction_seconds')), row
            for stage, total in zip(stage_rows[3:], lines[1:]):
                assert float(stage['matrix_seconds']) + float(stage['reconstruction_seconds']) <= float(total.split('\t')[1]) + 2e-9
    # Real C diagnostics are opt-in and do not contaminate the result table.
    output = base / 'progress'
    result = subprocess.run([sys.executable, str(root / 'scripts/rpq_paths_bench.py'),
                             str(queries / '1'), str(matrices), '--process',
                             '--stage-times', '--memory',
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == (output / 'errors.log').read_text()
    assert '[process]' not in result.stdout
    with (output / 'stage_times.tsv').open() as stream:
        assert [row['query'] for row in csv.DictReader(stream, delimiter='\t')] == ['1']
    with (output / 'memory.tsv').open() as stream:
        assert [(row['query'], row['status']) for row in csv.DictReader(stream, delimiter='\t')] == [('1', 'ok')]
    stages = ['initializing LAGraph/GraphBLAS', 'initialization complete',
              'loading graph matrix 1/1', 'reading Matrix Market',
              'setting row-major storage', 'materializing', 'creating LAGraph graph',
              'ready (info=0)', 'graph loading complete', 'query 1: measured pass 1',
              'running algorithm', 'algorithm returned (info=0)']
    previous = -1
    for stage in stages:
        previous = result.stderr.index(stage, previous + 1)
    assert '[process]' not in (base / 'all-False/errors.log').read_text()
    # A named query folder is accepted directly, including paths with spaces.
    single = base / 'single query'
    single.mkdir()
    for path in (queries / '1').iterdir():
        (single / path.name).write_bytes(path.read_bytes())
    for verify in (False, True):
        for semantics in ('all', 'simple', 'trails', 'shortest'):
            output = base / f'single-{semantics}-{verify}'
            command = [sys.executable, str(root / 'scripts/rpq_paths_bench.py'),
                       str(single), str(matrices), '--semantics', semantics,
                       '--warmup', '1', '--iterations', '2', '--output', str(output), '--stage-times']
            if verify:
                command.append('--verify')
            subprocess.run(command, check=True)
            lines = (output / 'results.tsv').read_text().splitlines()
            assert [line.split('\t')[::2] for line in lines[1:]] == [['single query', '1']] * 2, lines
            with (output / 'stage_times.tsv').open() as stream:
                assert [row['query'] for row in csv.DictReader(stream, delimiter='\t')] == ['single query'] * 3
            metadata = json.loads((output / 'run.json').read_text())
            assert metadata['exit_code'] == 0 and metadata['verify'] == verify
            assert metadata['build']['lagraph_commit'] == metadata['lagraph_commit']
    # Block the next query on a FIFO: the previous row must already be in
    # the output file while the driver is still running, even with buffering.
    import os
    import time
    blocked = base / 'blocked'
    blocked.mkdir()
    os.mkfifo(blocked / 'meta.txt')
    live_results = base / 'live.tsv'
    with live_results.open('w') as stream:
        process = subprocess.Popen([str(root / 'build/rpq_paths_benchmark'),
                                    'trails', '0', '1', '1', '7', str(matrices / '7.txt'),
                                    str(queries / '1'), str(blocked), str(queries / '4')],
                                   stdout=stream, stderr=subprocess.PIPE, text=True)
        writer = None
        try:
            deadline = time.monotonic() + 10
            while writer is None:
                try:
                    writer = os.open(blocked / 'meta.txt', os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    import errno
                    if exc.errno != errno.ENXIO:
                        raise
                    assert process.poll() is None, 'driver exited before next query'
                    assert time.monotonic() < deadline, 'driver did not reach next query'
                    time.sleep(0.01)
            assert process.poll() is None
            lines = live_results.read_text().splitlines()
            assert lines[0] == 'query\ttime_seconds\tpaths', lines
            assert len(lines) == 2 and lines[1].split('\t')[::2] == ['1', '1'], lines
            assert float(lines[1].split('\t')[1]) >= 0
            # Let the blocked query fail, then ensure later queries still run.
            os.write(writer, b'invalid metadata\n')
            os.close(writer)
            writer = None
            _, errors = process.communicate(timeout=10)
            assert process.returncode != 0 and 'blocked' in errors, errors
            lines = live_results.read_text().splitlines()
            assert [line.split('\t')[::2] for line in lines[1:]] == [['1', '1'], ['4', '1']], lines
        finally:
            if writer is not None:
                os.close(writer)
            if process.poll() is None:
                process.kill()
            process.communicate()
    # Python rejects unsupported endpoints before starting the C executable.
    fixed = queries / '5'
    fixed.mkdir()
    (fixed / 'meta.txt').write_text('1 3\n1 1\n1 2\n1 7\n')
    (fixed / '7.txt').write_text((queries / '1/7.txt').read_text())
    output = base / 'failed'
    result = subprocess.run([sys.executable, str(root / 'scripts/rpq_paths_bench.py'), str(queries),
                             str(matrices), '--output', str(output), '--verify'], capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
    assert 'fixed source and destination' in result.stderr
    import shutil
    shutil.rmtree(fixed)
    # Exercise archive download/extraction through local file URLs as well.
    qzip = shutil.make_archive(str(base / 'q'), 'zip', queries)
    mzip = shutil.make_archive(str(base / 'm'), 'zip', matrices)
    subprocess.run([sys.executable, str(root / 'scripts/download_data.py'),
                    '--queries-url', Path(qzip).as_uri(), '--matrices-url', Path(mzip).as_uri(),
                    '--output', str(base / 'downloaded'), '--verify'], check=True)
print('Smoke tests passed: single/batch queries, four semantics with/without --verify, inverse/backward queries, any-any filtering, repeated passes, download.')
