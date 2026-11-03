# RPQ path benchmarks

Standalone C driver for the predecessor-based `LAGraph_2RpqPath_*` API.
GraphBLAS and LAGraph are Git submodules pinned to commits, not moving branches.
The driver loads inputs, measures only the public algorithm call, and releases
returned paths outside the timed region. It does not use LAGraph demo/test code.
Requires Git, CMake 3.23+, a C11 compiler and Python 3.9+; OpenMP is recommended.

```bash
git submodule update --init --recursive
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target rpq_paths_benchmark --parallel 4
python3 scripts/rpq_paths_bench.py /path/to/wikidata-queries-mm /path/to/wikidata-mm \
  --semantics trails --warmup 1 --iterations 5 --output results/trails
```

`--output` creates a directory containing `results.tsv`, `errors.log` and
`run.json` (command, dependency commits, build configuration and exit status).
Existing files with those names are replaced. Without `--output`, results go to
stdout and diagnostics to stderr. An unsuccessful query makes the exit status
nonzero; successful queries still appear in the table. `--build-dir` selects a
build directory. `--semantics` accepts `all`, `simple`, `trails`, `shortest`.
Set `LAGRAPH_RPQ_TRACE=1` to trace each query/pass in the error log.

Each row contains the query number, mean measured time in seconds, and path
count. Warmup runs complete before measured passes. Graph matrices are loaded
once; NFA loading and result cleanup are outside the timer. All passes use fresh
algorithm workspace. Different path counts between measured passes are errors.

## Inputs

Graph matrices are square Matrix Market **coordinate** files named after positive
numeric labels: `141.txt`, `62.mtx`, etc. Directory scans accept `.txt`, `.mtx`,
`.mat` and ignore auxiliary files such as `vertices.txt`. Individual matrices and
`141=/path/to/file` mappings are also accepted. Matching dimensions are required
within each query. No vertex renumbering, padding, or resizing is performed.

Queries are numbered directories with `meta.txt` and signed `<label>.txt` NFA
matrices. `raw.txt` is ignored. Metadata example:

```text
12 0
1 1
1 3
2 141 -62
```

The first line holds source/destination vertex IDs: one-based, with `0` meaning
unrestricted. The next two counted lists are initial/final NFA states, also
one-based. The last counted list holds signed labels; `-62.txt` uses graph
matrix `62.*` with `inverse_labels=true`. A fixed destination runs backward,
swapping initial/final sets and setting `inverse=true`. Two free endpoints use
all vertices as sources. Two fixed endpoints are rejected because the API has
no destination filter. The launcher validates metadata, label availability,
matrix headers, dimensions and state/endpoint bounds before running; the C
Matrix Market reader validates matrix entries on load.

The [PR #13 test fixtures](https://github.com/SparseLinearAlgebra/LAGraph/pull/13/files)
use a different harness format: `<n>_meta.txt` contains only the two counted NFA
state lists; `<n>_sources.txt` contains an uncounted list of source vertices.
Those files are not interchangeable with Wikidata `meta.txt`. Both formats use
one-based IDs on disk; `LAGraph_MMRead` converts matrix indices to zero-based,
and the driver converts metadata IDs once. The public API takes arrays of graph
and NFA matrices, state/source arrays and direction flags, rather than filenames.

## Downloading prepared data

No dataset URL is assumed. Supply tar/ZIP archives of prepared query and graph
matrix directories (HTTP(S) or `file://` URLs):

```bash
python3 scripts/download_data.py --queries-url "$QUERIES_URL" \
  --matrices-url "$MATRICES_URL" --output data/wikidata
python3 scripts/rpq_paths_bench.py data/wikidata/queries data/wikidata/matrices \
  --output results/wikidata
```

Archives may contain a wrapper directory. Downloads stream to temporary files;
extraction rejects traversal paths, links and special tar entries. Inputs are
validated before publishing the output directory. Existing output directories
are refused. Matrices and query metadata are preserved without conversion.
The script downloads prepared Matrix Market data; it does not convert RDF/SPARQL.

## Updating dependencies

Initial pins: LAGraph `2e8965e08f3c5bdfaabdcd77daf6cc299d570445`,
GraphBLAS `ea79e354877ad5e96fad13e161b8db6d661875e4` (10.5.0).
The LAGraph pin includes the predecessor-based RPQ path API. To select another
commit, fetch and check it out explicitly, rebuild, and commit the updated gitlink:

```bash
git -C LAGraph fetch origin
git -C LAGraph checkout <commit>
git add LAGraph
git commit -m 'Update LAGraph pin'
cmake -S . -B build
cmake --build build --target rpq_paths_benchmark --parallel 4
```

For another machine, the pinned commits must be available in the submodule
remotes. Local changes in the original sibling LAGraph checkout are not copied
into the submodule. Changes to a submodule checkout require rebuilding.

The pinned algorithm returns at most 100000 distinct paths with a maximum length
of 60000 edges by default. Hitting the count cap returns success with a prefix;
`shortest` compares only that prefix and may miss shorter paths. Benchmark path
counts therefore inherit these limits. Backward `shortest` uses minima per
original source endpoint.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 tests/smoke.py
```

The smoke test checks all four semantics, inverse labels, backward/free endpoints,
warmup and measured passes, output artifacts, and archive loading on tiny graphs.
For a faster development build, configure with `-DGRAPHBLAS_COMPACT=ON` to skip
FactoryKernels; JIT remains enabled. Record this option when comparing timings.
