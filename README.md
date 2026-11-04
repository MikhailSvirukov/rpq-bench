# RPQ path benchmarks

Benchmark for the `LAGraph_2RpqPath_*` API, with a standalone C driver.
GraphBLAS and LAGraph are submodules pinned to commits.
Requires Git, CMake 3.23+, a C11 compiler and Python 3.9+.

## Build and run

```bash
git submodule update --init --recursive
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target rpq_paths_benchmark --parallel 4
python3 scripts/rpq_paths_bench.py /path/to/queries /path/to/matrices \
  --semantics trails --warmup 1 --iterations 5 --output results/trails
```

The first argument accepts either a batch directory with numbered query
subfolders, or one query folder containing `meta.txt` and its NFA label
matrices. A single query folder may have any name. For example:

```bash
python3 scripts/rpq_paths_bench.py /path/to/queries/1 /path/to/matrices \
  --semantics trails --warmup 1 --iterations 5 --output results/query-1
```

The second argument accepts a graph matrix directory, individual numeric
matrix files, or `label=/path/to/matrix` mappings. A matrix directory is scanned
for positive numeric `.txt`, `.mtx`, and `.mat` filenames.

`--semantics`: `all`, `simple`, `trails`, or `shortest`.
`--build-dir` selects the build directory (default: this repository's `build`).
`--output` writes `results.tsv`, `errors.log`, and `run.json` with the command,
dependency commits, build configuration and exit status. Existing files are
replaced. Without it, results go to stdout and errors to stderr.

Times are means over measured passes; warmup passes finish first. Only the
public algorithm call is timed, including its workspace setup and path
reconstruction. File reading and freeing returned paths are outside the timer.
Graph matrices are loaded once; each invocation reloads its NFA. Failed queries
are omitted from the results and cause a nonzero exit status; other queries
continue. Set `LAGRAPH_RPQ_TRACE=1` to log the active query and pass.

Add `--verify` to check input files and repeated path counts in Python.
Verification is disabled by default; the C algorithm assumes valid inputs.
