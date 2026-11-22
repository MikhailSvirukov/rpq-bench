#!/usr/bin/env python3
"""Run predecessor-based RPQ benchmarks on prepared Matrix Market inputs."""

import argparse
import csv
from pathlib import Path
import subprocess
import sys
import json
import os
import math
import signal
import tempfile
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from itertools import islice

from inputs import validate_queries, validate_samples


class BenchmarkInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def is_unbound_query(directory):
    """Skip only a confirmed any-any query; leave invalid inputs to validation/the driver."""
    try:
        with (directory / "meta.txt").open() as stream:
            endpoints = list(islice((token for line in stream for token in line.split()), 2))
        return len(endpoints) == 2 and all(int(value) == 0 for value in endpoints)
    except (OSError, ValueError):
        return False


def forward_lines(source, *destinations):
    for line in source:
        for destination in destinations:
            if destination is not None:
                destination.write(line)
                destination.flush()


def run_driver(command, results, stderr, environment, progress=False, monitor=False, skip_header=False):
    # Drain stderr independently: loading can produce progress before any stdout.
    with subprocess.Popen(command, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE if progress else stderr,
                          env=environment, text=True) as process:
        with ThreadPoolExecutor(max_workers=1) as executor:
            errors = (executor.submit(forward_lines, process.stderr, stderr, sys.stderr)
                      if progress else None)
            try:
                for line in process.stdout:
                    if skip_header and line == "query\ttime_seconds\tpaths\n":
                        continue
                    for stream in (results, sys.stdout):
                        if stream is not None:
                            stream.write(line)
                            stream.flush()
                status = process.wait()
                if errors is not None:
                    errors.result()
                return status
            except BaseException:
                # Let the memory monitor stop and reap its C child and close reports.
                if monitor:
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                else:
                    process.kill()
                    process.wait()
                raise


def read_invocations(path):
    """A killed process may leave a partial final record; ignore only that tail."""
    records = []
    if path.exists():
        for line in path.read_text().splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            event, index, info = line.rstrip("\n").split("\t")
            records.append((event, int(index), int(info)))
    return records


def run_recovering(command, queries, args, results, stderr, environment, metadata, stack):
    total = len(queries) * (args.warmup + args.iterations)
    resume = overall_status = 0
    attempts = metadata["attempts"] = []
    failures = None
    if args.output is not None:
        failure_stream = stack.enter_context((args.output / "failures.tsv").open("w"))
        failures = csv.writer(failure_stream, delimiter="\t", lineterminator="\n")
        failures.writerow(("invocation", "query", "phase", "pass", "status", "exit_code", "info"))
    temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rpq-resume-")))

    def failure(index, reason, status, info=""):
        phase = "warmup" if index < args.warmup * len(queries) else "measured"
        offset = 0 if phase == "warmup" else args.warmup * len(queries)
        row = (index, queries[index % len(queries)].name, phase,
               (index - offset) // len(queries) + 1, reason, status, info)
        if failures is not None:
            failures.writerow(row)
            failure_stream.flush()
        print("benchmark: query {}: {} pass {}: {} (exit={}, info={})".format(
            row[1], phase, row[3], reason, status, info),
            file=stderr if stderr is not None else sys.stderr, flush=True)

    while resume < total:
        journal = temporary / f"attempt-{len(attempts)}.tsv"
        launch = [command[0], "--resume-at", str(resume), "--invocation-log", str(journal), *command[1:]]
        if args.memory:
            launch = [sys.executable, str(Path(__file__).with_name("memory_monitor.py")),
                      "--output", str(args.output.resolve()),
                      "--interval-ms", str(args.memory_interval_ms),
                      "--expected-invocations", str(total - resume),
                      *(["--append"] if attempts else []), "--", *launch]
        status = run_driver(launch, results, stderr, environment, args.process,
                            monitor=args.memory, skip_header=bool(attempts))
        status = status if status >= 0 else 128 - status
        records = read_invocations(journal)
        attempts.append({"resume_at": resume, "exit_code": status,
                         "last_invocation": records[-1][1] if records else None})
        for event, index, info in records:
            if event == "end" and info != 0:
                failure(index, "algorithm_error", status, info)
        if status:
            overall_status = overall_status or status
        # Ordinary API/input errors are handled by the C loop. SIGINT/SIGTERM
        # are user cancellation, and an exit before any journal entry cannot
        # be attributed to a query (e.g. graph loading); never restart those.
        if status < 128 or status in (130, 143) or not records:
            break
        event, index, _ = records[-1]
        if event == "begin":
            failure(index, "sigkill" if status == 137 else "process_signal", status)
        resume = index + 1
        if resume < total:
            print(f"benchmark: restarting driver at invocation {resume}; graph will be reloaded",
                  file=stderr if stderr is not None else sys.stderr, flush=True)
    return overall_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", type=Path,
                        help="query folder containing meta.txt, or directory of numbered query folders")
    parser.add_argument("matrices", nargs="+",
                        help="matrix directory, individual <label>.txt/.mtx files, or <label>=<path>")
    parser.add_argument("--semantics", choices=("all", "simple", "trails", "shortest"),
                        default="all")
    parser.add_argument("--warmup", type=int, default=0,
                        help="unreported passes over the full batch (default: 0)")
    parser.add_argument("--iterations", type=int, default=1,
                        help="measured passes over the full batch (default: 1)")
    parser.add_argument("--build-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build",
                        help="benchmark CMake build directory (default: build)")
    parser.add_argument("--output", type=Path,
                        help="also save stdout results to results.tsv, plus errors.log and run.json")
    parser.add_argument("--max-path-count", type=int, default=100000,
                        help="maximum distinct answers per invocation (default: 100000)")
    parser.add_argument("--max-path-length", type=int, default=60000,
                        help="maximum path length in edges, inclusive (default: 60000)")
    parser.add_argument("--jit-cache", type=Path, help="GraphBLAS JIT cache directory (created if necessary)")
    parser.add_argument("--verify", action="store_true",
                        help="verify input files and repeated path counts in Python")
    parser.add_argument("--process", action="store_true",
                        help="show loading stages and active queries on stderr, also with --output")
    parser.add_argument("--memory", action="store_true",
                        help="sample driver RSS and virtual memory in a separate process (Linux; requires --output)")
    parser.add_argument("--memory-interval-ms", type=float, default=20,
                        help="memory sampling interval in milliseconds (default: 20)")
    args = parser.parse_args()
    if args.warmup < 0 or args.iterations < 1:
        parser.error("--warmup must be nonnegative and --iterations must be positive")
    if not math.isfinite(args.memory_interval_ms) or args.memory_interval_ms <= 0:
        parser.error("--memory-interval-ms must be finite and positive")
    if args.memory and args.output is None:
        parser.error("--memory requires --output")
    if args.memory and (not sys.platform.startswith("linux") or not Path("/proc/self/status").is_file()):
        parser.error("--memory requires Linux procfs mounted at /proc")
    driver = args.build_dir / "rpq_paths_benchmark"
    if not args.queries.is_dir():
        parser.error(f"not a queries directory: {args.queries}")
    if (args.queries / "meta.txt").is_file():
        queries = [args.queries]
    else:
        queries = sorted((p for p in args.queries.iterdir()
                          if p.is_dir() and p.name.isdecimal()),
                         key=lambda p: int(p.name))
    if not queries:
        parser.error("expected meta.txt or numbered query folders containing meta.txt")
    queries = [directory for directory in queries if not is_unbound_query(directory)]
    if not queries:
        return 0
    if not driver.is_file():
        parser.error(f"missing {driver}; build the rpq_paths_benchmark CMake target first")
    inputs = []
    for value in args.matrices:
        path = Path(value)
        if path.is_dir():
            files = sorted((p for p in path.iterdir()
                            if p.is_file() and p.stem.isdecimal() and int(p.stem) > 0
                            and p.suffix.lower() in (".txt", ".mtx", ".mat")),
                           key=lambda p: (int(p.stem), p.name))
            if args.verify and not files:
                parser.error(f"no numeric matrix files (.txt/.mtx/.mat) in directory: {path}")
            inputs.extend(str(p) for p in files)
        else:
            inputs.append(value)
    matrices = {}
    for value in inputs:
        label, separator, filename = value.partition("=")
        path = Path(filename if separator else value)
        if not separator:
            label = path.stem
        if args.verify and (not label or "/" in label or "\\" in label or not path.is_file()):
            parser.error(f"expected a matrix directory, <label>.txt/.mtx, or <label>=<existing-file>: {value}")
        if args.verify and label in matrices:
            parser.error(f"duplicate graph label: {label}")
        if args.verify and (not label.isdecimal() or int(label) < 1 or int(label) > 2**63 - 1):
            parser.error(f"expected a positive integer graph label: {label}")
        label = str(int(label))
        if args.verify and label in matrices:
            parser.error(f"duplicate graph label: {label}")
        matrices[label] = path
    if args.verify:
        if args.process:
            print("[process] verifying inputs", file=sys.stderr, flush=True)
        try:
            validate_queries(queries, matrices)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    if args.verify and args.process:
        print("[process] input verification complete", file=sys.stderr, flush=True)
    if args.max_path_count < 1 or args.max_path_length < 0:
        parser.error("--max-path-count must be positive; --max-path-length must be nonnegative")
    command = [str(driver.resolve()), args.semantics, str(args.warmup),
               str(args.iterations), str(len(matrices))]
    for label, path in matrices.items():
        command.extend([label, str(path.resolve())])
    command.extend(str(directory.resolve()) for directory in queries)
    command[1:1] = ["--max-path-count", str(args.max_path_count),
                    "--max-path-length", str(args.max_path_length)]
    jit_cache = None
    if args.jit_cache is not None:
        jit_cache = args.jit_cache.expanduser().resolve()
        try:
            jit_cache.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            parser.error(f"cannot create JIT cache {jit_cache}: {exc}")
        command[1:1] = ["--jit-cache", str(jit_cache)]
    with ExitStack() as stack:
        results = stderr = None
        if args.output is not None:
            args.output.mkdir(parents=True, exist_ok=True)
            command[1:1] = ["--jit-error-log", str((args.output / "jit-errors.log").resolve())]
            results = stack.enter_context((args.output / "results.tsv").open("w"))
            stderr = stack.enter_context((args.output / "errors.log").open("w"))
        root = Path(__file__).resolve().parents[1]
        def revision(path):
            result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        build_info = args.build_dir / "build-info.json"
        metadata = {"command": command, "semantics": args.semantics,
                    "verify": args.verify, "process": args.process,
                    "memory": args.memory,
                    "jit_error_log": str((args.output / "jit-errors.log").resolve()) if args.output is not None else None,
                    "jit_cache": str(jit_cache) if jit_cache is not None else None,
                    "max_path_count": args.max_path_count, "max_path_length": args.max_path_length,
                    "warmup": args.warmup, "iterations": args.iterations,
                    "lagraph_commit": revision(root / "LAGraph"),
                    "graphblas_commit": revision(root / "GraphBLAS")}
        if build_info.is_file():
            metadata["build"] = json.loads(build_info.read_text())
        if args.memory:
            metadata["memory_profile"] = {
                "interval_ms": args.memory_interval_ms,
                "scope": "algorithm call; whole driver process",
                "source": "/proc/<pid>/status: VmRSS, VmSize",
                "clock": "monotonic_ns",
                "boundaries": "stderr marker receipt times",
                "summary": "memory.tsv", "samples": "memory_samples.tsv",
                "events": "memory_events.tsv"}
        environment = os.environ.copy()
        environment.pop("RPQ_BENCH_VERIFY_SAMPLES", None)
        environment.pop("RPQ_BENCH_PROCESS", None)
        if args.process or args.memory:
            environment["RPQ_BENCH_PROCESS"] = "1"
        if args.verify:
            samples = ((args.output / "samples.tsv") if args.output is not None else
                       Path(stack.enter_context(tempfile.TemporaryDirectory())) / "samples.tsv")
            environment["RPQ_BENCH_VERIFY_SAMPLES"] = str(samples.resolve())
        previous_handlers = {}
        def interrupted(signum, frame):
            raise BenchmarkInterrupted(signum)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            status = run_recovering(command, queries, args, results, stderr,
                                    environment, metadata, stack)
        except BenchmarkInterrupted as exc:
            status = 128 + exc.signum
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        if args.verify and samples.is_file():
            try:
                validate_samples(samples)
            except (ValueError, OSError) as exc:
                print(f"verification: {exc}", file=stderr if stderr is not None else sys.stderr)
                status = 1
        if args.output is not None:
            metadata["exit_code"] = status
            (args.output / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return status


if __name__ == "__main__":
    sys.exit(main())
