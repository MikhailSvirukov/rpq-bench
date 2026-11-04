"""Linux memory sampler and stderr marker reader, run as a separate process.

The monitor owns the driver so it is ready before the first query. Stdout is
inherited; stderr is parsed and forwarded. No driver/algorithm changes or IPC
handshake are required. Marker receipt times are approximate query boundaries.
"""

import argparse
import codecs
import csv
from contextlib import ExitStack
import locale
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time


QUERY = re.compile(r"query (.+): (warmup|measured) pass ([0-9]+)$")
BEGIN = re.compile(r"\[process\] query (.+): running algorithm$")
END = re.compile(r"\[process\] query (.+): algorithm returned \(info=(-?[0-9]+)\)$")
SUMMARY_FIELDS = ("query", "phase", "pass", "status", "info", "start_ns", "end_ns",
                  "samples", "rss_min_bytes", "rss_max_bytes", "vms_min_bytes", "vms_max_bytes")


class Report:
    def __init__(self, stream, fields):
        self.stream = stream
        self.writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        self.write(fields)

    def write(self, values):
        self.writer.writerow(values)
        self.stream.flush()


def read_memory(pid):
    """Return resident and virtual bytes; None when the process has exited."""
    try:
        with open(f"/proc/{pid}/status") as stream:
            values = {}
            for line in stream:
                key, _, value = line.partition(":")
                if key in ("VmRSS", "VmSize"):
                    values[key] = int(value.split()[0]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        return None
    # A zombie's status has no memory fields.
    if not values:
        return None
    return values["VmRSS"], values["VmSize"]


class QueryProfile:
    def __init__(self, summary, events):
        self.summary = summary
        self.events = events
        self.current = None
        self.completed = 0
        self.unfinished = 0

    def event(self, timestamp, name, info=""):
        row = self.current
        self.events.write((timestamp, name, row["query"], row["phase"], row["pass"], info))

    def finish(self):
        if self.current is None:
            return
        row = self.current
        if not row["status"]:
            row["status"] = "incomplete" if row["start_ns"] != "" else "not_started"
            self.unfinished += 1
        self.summary.write(row[field] for field in SUMMARY_FIELDS)
        self.completed += 1
        self.current = None

    def line(self, line, timestamp):
        match = QUERY.fullmatch(line)
        if match:
            self.finish()
            self.current = dict.fromkeys(SUMMARY_FIELDS, "")
            self.current.update({"query": match[1], "phase": match[2],
                                 "pass": int(match[3]), "samples": 0})
            self.event(timestamp, "query_begin")
            return
        begin, end = BEGIN.fullmatch(line), END.fullmatch(line)
        if not begin and not end:
            return
        match = begin or end
        if self.current is None or Path(match[1]).name != self.current["query"]:
            raise ValueError(f"query marker without matching invocation: {line}")
        row = self.current
        if begin:
            if row["start_ns"] != "":
                raise ValueError(f"duplicate algorithm start: {line}")
            row["start_ns"] = timestamp
            self.event(timestamp, "algorithm_begin")
        else:
            if row["start_ns"] == "":
                raise ValueError(f"algorithm end without start: {line}")
            row.update(end_ns=timestamp, info=int(end[2]),
                       status="ok" if int(end[2]) == 0 else "algorithm_error")
            self.event(timestamp, "algorithm_end", row["info"])
            self.finish()

    def sample(self, timestamp, rss, vms):
        row = self.current
        if row is None or row["start_ns"] == "" or timestamp < row["start_ns"]:
            return
        for name, value in (("rss", rss), ("vms", vms)):
            low, high = name + "_min_bytes", name + "_max_bytes"
            row[low] = value if row[low] == "" else min(row[low], value)
            row[high] = value if row[high] == "" else max(row[high], value)
        row["samples"] += 1


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupt(signum, frame):
    raise Interrupted(signum)


def stop_driver(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def monitor(command, output, interval_ms, expected_invocations):
    with ExitStack() as stack:
        def report(name, fields):
            return Report(stack.enter_context((output / name).open("w", newline="")), fields)
        summary = report("memory.tsv", SUMMARY_FIELDS)
        samples = report("memory_samples.tsv", ("timestamp_ns", "rss_bytes", "vms_bytes"))
        events = report("memory_events.tsv", ("timestamp_ns", "event", "query", "phase", "pass", "info"))
        profile = QueryProfile(summary, events)
        selector = stack.enter_context(selectors.DefaultSelector())
        process = subprocess.Popen(command, stderr=subprocess.PIPE, bufsize=0)
        # Cleanup also runs on SIGINT/SIGTERM and report/pipe errors.
        try:
            selector.register(process.stderr, selectors.EVENT_READ)
            decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace")
            pending = ""
            interval_ns = max(1, int(interval_ms) * 1_000_000 + int(interval_ms % 1 * 1_000_000))
            next_sample = time.monotonic_ns()
            stderr_open = True
            while stderr_open or process.poll() is None:
                # Poll exit even when stderr closes early and the interval is long.
                timeout = min(0.1, max(0, (next_sample - time.monotonic_ns()) / 1_000_000_000))
                ready = selector.select(timeout)
                for key, _ in ready:
                    chunk = os.read(key.fd, 65536)
                    received = time.monotonic_ns()
                    pending += decoder.decode(chunk, final=not chunk)
                    lines = pending.split("\n")
                    pending = lines.pop()
                    if not chunk:
                        selector.unregister(process.stderr)
                        stderr_open = False
                        if pending:
                            lines.append(pending)
                            pending = ""
                    for line in lines:
                        # Use the receipt timestamp before forwarding (which may block).
                        profile.line(line, received)
                        sys.stderr.write(line + "\n")
                    sys.stderr.flush()
                now = time.monotonic_ns()
                if now >= next_sample:
                    memory = read_memory(process.pid)
                    timestamp = time.monotonic_ns()
                    if memory is not None:
                        samples.write((timestamp, *memory))
                        profile.sample(timestamp, *memory)
                    # Skip missed ticks; never fabricate or burst catch-up samples.
                    next_sample += ((timestamp - next_sample) // interval_ns + 1) * interval_ns
            status = process.wait()
        finally:
            # Repeated terminal signals must not interrupt child cleanup.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            stop_driver(process)
            process.stderr.close()
            profile.finish()
        if status == 0 and (profile.completed != expected_invocations or profile.unfinished):
            print("memory monitor: missing query/algorithm markers; check that the C driver "
                  "supports RPQ_BENCH_PROCESS (rebuild if needed)", file=sys.stderr)
            return 1
        return status if status >= 0 else 128 - status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=float, required=True)
    parser.add_argument("--expected-invocations", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not math.isfinite(args.interval_ms) or args.interval_ms <= 0:
        parser.error("a command and a finite positive interval are required")
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return monitor(command, args.output, args.interval_ms, args.expected_invocations)
    except Interrupted as exc:
        return 128 + exc.signum
    except (OSError, ValueError) as exc:
        print(f"memory monitor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
