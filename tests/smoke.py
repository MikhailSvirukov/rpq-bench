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
    for semantics in ('all', 'simple', 'trails', 'shortest'):
        output = base / semantics
        subprocess.run([sys.executable, str(root / 'scripts/rpq_paths_bench.py'), str(queries), str(matrices),
                        '--build-dir', str(root / 'build'), '--semantics', semantics,
                        '--warmup', '1', '--iterations', '2', '--output', str(output)], check=True)
        lines = (output / 'results.tsv').read_text().splitlines()
        assert lines[0] == 'query\ttime_seconds\tpaths', lines
        assert [line.split('\t')[::2] for line in lines[1:]] == [['1', '1'], ['2', '1'], ['3', '2'], ['4', '1']], lines
        assert json.loads((output / 'run.json').read_text())['exit_code'] == 0
    # Unsupported two-fixed-endpoint query fails alone; other results survive.
    fixed = queries / '5'
    fixed.mkdir()
    (fixed / 'meta.txt').write_text('1 3\n1 1\n1 2\n1 7\n')
    (fixed / '7.txt').write_text((queries / '1/7.txt').read_text())
    output = base / 'failed'
    result = subprocess.run([sys.executable, str(root / 'scripts/rpq_paths_bench.py'), str(queries),
                             str(matrices), '--output', str(output)])
    assert result.returncode == 1
    assert len((output / 'results.tsv').read_text().splitlines()) == 5
    assert 'fixed source and destination' in (output / 'errors.log').read_text()
    # Exercise archive download/extraction through local file URLs as well.
    import shutil
    qzip = shutil.make_archive(str(base / 'q'), 'zip', queries)
    mzip = shutil.make_archive(str(base / 'm'), 'zip', matrices)
    subprocess.run([sys.executable, str(root / 'scripts/download_data.py'),
                    '--queries-url', Path(qzip).as_uri(), '--matrices-url', Path(mzip).as_uri(),
                    '--output', str(base / 'downloaded')], check=True)
print('Smoke tests passed: four semantics, inverse labels, backward/free endpoints, repeated passes, download.')
