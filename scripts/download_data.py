#!/usr/bin/env python3
"""Download query/matrix tar or ZIP archives and validate their input format."""
import argparse
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

from inputs import validate_queries


def extract(archive, destination):
    """Extract regular files and directories only, without escaping destination."""
    destination = destination.resolve()
    def target(name):
        path = (destination / name).resolve()
        if not path.is_relative_to(destination):
            raise ValueError(f"unsafe archive path: {name}")
        return path
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                path = target(member.filename)
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError(f"archive symlink: {member.filename}")
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as src, path.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as source:
            for member in source:
                path = target(member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as src, path.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                else:
                    raise ValueError(f"archive contains a link or special file: {member.name}")


def locate(root, queries=False):
    candidates = {p.parent for p in root.rglob('meta.txt')} if queries else {
        p.parent for p in root.rglob('*') if p.is_file() and p.stem.isdecimal()
        and p.suffix.lower() in ('.txt', '.mtx', '.mat')}
    if queries:
        candidates = {p.parent for p in candidates if p.name.isdecimal()}
    if len(candidates) != 1:
        raise ValueError(f"{root}: expected exactly one data directory, found {len(candidates)}")
    return candidates.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queries-url', required=True, help='tar/ZIP archive of numbered query directories')
    parser.add_argument('--matrices-url', required=True, help='tar/ZIP archive of numeric graph label matrices')
    parser.add_argument('--output', type=Path, default=Path('data'), help='new data directory')
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f'{args.output} already exists; choose a new --output directory')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=args.output.parent) as temporary:
            stage = Path(temporary)
            roots = []
            for kind, url in [('queries', args.queries_url), ('matrices', args.matrices_url)]:
                archive = stage / f'{kind}.archive'
                request = urllib.request.Request(url, headers={'User-Agent': 'rpq-benchmark'})
                with urllib.request.urlopen(request, timeout=120) as src, archive.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                root = stage / kind
                root.mkdir()
                extract(archive, root)
                roots.append(locate(root, queries=kind == 'queries'))
            queries, matrices = roots
            query_dirs = sorted(p for p in queries.iterdir() if p.is_dir() and p.name.isdecimal())
            graph = {}
            for path in matrices.iterdir():
                if path.is_file() and path.stem.isdecimal() and path.suffix.lower() in ('.txt', '.mtx', '.mat'):
                    key = str(int(path.stem))
                    if key == '0' or key in graph:
                        raise ValueError(f'duplicate or zero graph label: {path}')
                    graph[key] = path
            validate_queries(query_dirs, graph)
            ready = stage / 'ready'
            ready.mkdir()
            shutil.move(str(queries), ready / 'queries')
            shutil.move(str(matrices), ready / 'matrices')
            ready.rename(args.output)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, f'{exc}\n')
    print(f'Validated data saved to {args.output}')


if __name__ == '__main__':
    main()
