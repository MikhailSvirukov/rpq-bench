#!/usr/bin/env python3
"""Integration test using the compiled public API, no external datasets."""
import json
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
                       '--warmup', '1', '--iterations', '2', '--output', str(output)]
            if verify:
                command.append('--verify')
            subprocess.run(command, check=True)
            lines = (output / 'results.tsv').read_text().splitlines()
            assert lines[0] == 'query\ttime_seconds\tpaths', lines
            assert [line.split('\t')[::2] for line in lines[1:]] == [['1', '1'], ['2', '1'], ['3', '2'], ['4', '1']], lines
            metadata = json.loads((output / 'run.json').read_text())
            assert metadata['exit_code'] == 0 and metadata['verify'] == verify
            assert (output / 'samples.tsv').exists() == verify
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
print('Smoke tests passed: four semantics with/without --verify, inverse/backward/free queries, repeated passes, download.')
