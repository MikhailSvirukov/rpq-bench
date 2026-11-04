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

Queries whose source and destination in `meta.txt` are both zero (`any-any`)
are silently skipped in Python before verification or driver startup. No log
entries or report records are created for them. If all queries are skipped,
the command succeeds without launching the driver or creating output files.
This filter retains queries with at least one fixed endpoint; the existing
`--verify` check still rejects two fixed endpoints as unsupported by the API.

The second argument accepts a graph matrix directory, individual numeric
matrix files, or `label=/path/to/matrix` mappings. A matrix directory is scanned
for positive numeric `.txt`, `.mtx`, and `.mat` filenames.

`--semantics`: `all`, `simple`, `trails`, or `shortest`.
`--build-dir` selects the build directory (default: this repository's `build`).
Results always appear on stdout as each measured query finishes.
`--output` additionally saves the same rows to `results.tsv`, redirects errors
to `errors.log`, and writes `run.json` with the command, dependency commits,
build configuration and exit status. Existing files are replaced. Without
`--output`, errors go to stderr.
Add `--process` to print initialization, each matrix's loading stages and the
active query to stderr, flushing every message immediately. With `--output`,
C driver diagnostics also appear in the terminal and are saved to `errors.log`.
Rebuild the C driver after updating to use these loading diagnostics.

`results.tsv` contains three tab-separated columns:

```text
query   time_seconds   paths
1       0.001234567    42
2       0.000456789    3
```

Each successful measured query invocation writes and flushes one row immediately,
before the next query starts. Times are individual measurements in seconds,
not averages. With `--iterations 5`, each query produces five rows in pass order.
Warmup passes finish first and produce no result rows. Completed rows remain
available if a later query fails or the process is interrupted. Use
`tail -f results/trails/results.tsv` to watch results during a run.

Only the
public algorithm call is timed, including its workspace setup and path
reconstruction. File reading and freeing returned paths are outside the timer.
Graph matrices are loaded once; each invocation reloads its NFA. Failed invocations
produce no result row and cause a nonzero exit status; other queries
continue. Set `LAGRAPH_RPQ_TRACE=1` to log the active query and pass.

Add `--verify` to check input files and repeated path counts in Python.
Verification is disabled by default; the C algorithm assumes valid inputs.

## Algorithm stage times

Add `--stage-times --output <directory>` to write `stage_times.tsv`:

```bash
python3 scripts/rpq_paths_bench.py /path/to/queries /path/to/matrices \
  --semantics trails --warmup 1 --iterations 5 \
  --stage-times --output results/trails
```

The algorithm itself appends and flushes a row after each invocation, including
warmups, in batch/pass order. Columns are `query`, `matrix_seconds`, and
`reconstruction_seconds`. `query` is the query folder's name, preserved across
repetitions. The first timer surrounds `LG_RpqPath_Traverse` (building P);
the second surrounds `LG_RpqPath_Process` (path reconstruction and filtering).
Internal workspace/roots initialization, final query cleanup, and file I/O are
outside these two timers. A stage that fails reports its elapsed time; a stage
not reached has an empty field. Queries that fail before entering the algorithm
have no stage row. Existing output is replaced when a run starts.

The existing `time_seconds` still covers the whole public query call, so it
includes the timing file I/O when this option is enabled and need not equal the
sum of the two stage times. The option can be combined with `--memory`.
Rebuild the C driver and LAGraph after updating the submodule to use it.

The launcher sets `LAGRAPH_RPQ_STAGE_TIMINGS` to the output path; the C driver
sets `LAGRAPH_RPQ_QUERY_ID` before each invocation. Library-only callers can set
these two variables themselves; the library appends headerless TSV rows and
reports file errors through its return code. Use a separate file per process.

## Memory sampling (Linux)

Add `--memory --output <directory>` to sample the C driver's resident memory
(RSS) and virtual address space in a separate Python monitor process. Sampling
defaults to every 20 ms; `--memory-interval-ms` accepts a positive finite value:

```bash
python3 scripts/rpq_paths_bench.py /path/to/queries /path/to/matrices \
  --semantics trails --warmup 1 --iterations 5 \
  --memory --memory-interval-ms 20 --output results/trails
```

The graph remains loaded once in a single C process for the entire batch.
The monitor reads `VmRSS` and `VmSize` from `/proc/<driver-pid>/status`, converts
them to bytes, and uses the driver's existing stderr progress markers to delimit
each algorithm call. No C source or algorithm changes are needed. The driver
must support the current `RPQ_BENCH_PROCESS` markers; missing markers cause a
profiling error instead of a successful empty report. Memory mode enables those
markers automatically and saves them in `errors.log`; add `--process` to also
show them in the terminal. `results.tsv` and stdout keep their existing format.

Three additional files are written, replacing existing files of the same names:

- `memory.tsv`: one row per invocation, including warmups and repeated passes.
  Columns are `query`, `phase`, `pass`, `status`, `info`, `start_ns`, `end_ns`,
  `samples`, `rss_min_bytes`, `rss_max_bytes`, `vms_min_bytes`, `vms_max_bytes`.
  `info` is the algorithm's return code. Status is `ok`, `algorithm_error`,
  `not_started` (no algorithm start observed), or `incomplete` (no return observed).
  Missing boundaries/codes and extrema without samples are empty, not zero.
  Rows are flushed when the algorithm returns, a new query starts, or the
  monitor exits, including normal failure and interrupt cleanup.
- `memory_samples.tsv`: raw `timestamp_ns`, `rss_bytes`, `vms_bytes` samples
  throughout the driver's lifetime, including loading and cleanup.
- `memory_events.tsv`: `timestamp_ns`, `event`, `query`, `phase`, `pass`, `info`
  for received `query_begin`, `algorithm_begin`, and `algorithm_end` markers.

All timestamps use the monitor's monotonic clock in nanoseconds. Summary
extrema use only samples between the received algorithm start and end markers;
loading the query's NFA and freeing results are outside that interval. Sampling
settings, scope, and filenames are also recorded in `run.json`.

These are **sampled extrema**, not guaranteed peaks. Scheduler delays can make
the actual interval longer; missed ticks are skipped. A query shorter than the
interval can have zero samples. Marker receipt is asynchronous, so attribution
near a boundary is approximate. RSS accounting in `/proc` is itself approximate.
Both metrics describe the whole driver process, including the graph, libraries,
and allocations retained from earlier queries; they do not isolate allocations
made by one query. Virtual address space is not physical RAM usage. Monitoring
and progress output add overhead. The monitor's own memory is excluded.
