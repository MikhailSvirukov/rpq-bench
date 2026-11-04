#!/usr/bin/env python3
"""Run predecessor-based RPQ benchmarks on prepared Matrix Market inputs."""

import argparse
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

from inputs import validate_queries, validate_samples


class BenchmarkInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def forward_lines(source, *destinations):
    for line in source:
        for destination in destinations:
            destination.write(line)
            destination.flush()


def run_driver(command, results, stderr, environment, progress=False, monitor=False):
    if results is None:
        return subprocess.run(command, stderr=stderr, env=environment,
                              check=False).returncode
    # Drain stderr independently: loading can produce progress before any stdout.
    with subprocess.Popen(command, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE if progress else stderr,
                          env=environment, text=True) as process:
        with ThreadPoolExecutor(max_workers=1) as executor:
            errors = (executor.submit(forward_lines, process.stderr, stderr, sys.stderr)
                      if progress else None)
            try:
                forward_lines(process.stdout, results, sys.stdout)
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
    if not driver.is_file():
        parser.error(f"missing {driver}; build the rpq_paths_benchmark CMake target first")
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
    command = [str(driver.resolve()), args.semantics, str(args.warmup),
               str(args.iterations), str(len(matrices))]
    for label, path in matrices.items():
        command.extend([label, str(path.resolve())])
    command.extend(str(directory.resolve()) for directory in queries)
    with ExitStack() as stack:
        results = stderr = None
        if args.output is not None:
            args.output.mkdir(parents=True, exist_ok=True)
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
        launch_command = command
        if args.memory:
            launch_command = [sys.executable, str(Path(__file__).with_name("memory_monitor.py")),
                              "--output", str(args.output.resolve()),
                              "--interval-ms", str(args.memory_interval_ms),
                              "--expected-invocations", str(len(queries) * (args.warmup + args.iterations)),
                              "--", *command]
        previous_handlers = {}
        if args.memory:
            def interrupted(signum, frame):
                raise BenchmarkInterrupted(signum)
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            status = run_driver(launch_command, results, stderr, environment, args.process,
                                monitor=args.memory)
        except BenchmarkInterrupted as exc:
            status = 128 + exc.signum
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        if args.verify and status == 0:
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
