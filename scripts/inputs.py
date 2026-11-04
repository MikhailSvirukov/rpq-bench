"""Optional verification of prepared RPQ input files and measured samples."""
from pathlib import Path
import csv
import math


def matrix_shape(path):
    """Verify every coordinate entry, then return the square matrix dimension."""
    with Path(path).open() as stream:
        banner = stream.readline().lower().split()
        if (len(banner) != 5 or banner[:3] != ['%%matrixmarket', 'matrix', 'coordinate']
                or banner[3] not in ('pattern', 'integer', 'real')
                or banner[4] not in ('general', 'symmetric', 'skew-symmetric')):
            raise ValueError(f'{path}: expected a supported Matrix Market coordinate matrix')
        kind, storage = banner[3:]
        if kind == 'pattern' and storage == 'skew-symmetric':
            raise ValueError(f'{path}: pattern cannot be skew-symmetric')
        value_type = {'pattern': 'bool', 'integer': 'int64_t', 'real': 'double'}[kind]
        dimensions = None
        count = 0
        for line_number, line in enumerate(stream, 2):
            fields = line.split()
            if not fields:
                continue
            if fields[0].startswith('%'):
                if dimensions is None and fields[:2] == ['%%GraphBLAS', 'type']:
                    if len(fields) != 3:
                        raise ValueError(f'{path}:{line_number}: invalid GraphBLAS type')
                    value_type = fields[2]
                continue
            if dimensions is None:
                if len(fields) != 3 or any(not v.isascii() or not v.isdecimal() for v in fields):
                    raise ValueError(f'{path}:{line_number}: invalid matrix dimensions')
                rows, cols, entries = map(int, fields)
                if rows != cols:
                    raise ValueError(f'{path}: expected a square matrix')
                if rows > 2**60:
                    raise ValueError(f'{path}: matrix dimension exceeds GraphBLAS index range')
                integers = {f'{prefix}int{bits}_t': (0 if prefix else -(2**(bits-1)),
                            2**bits - 1 if prefix else 2**(bits-1) - 1)
                            for prefix in ('', 'u') for bits in (8, 16, 32, 64)}
                integers['bool'] = (0, 1)
                if value_type not in integers and value_type not in ('float', 'double'):
                    raise ValueError(f'{path}: unsupported GraphBLAS type {value_type}')
                if storage == 'skew-symmetric' and value_type in ('bool', 'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t'):
                    raise ValueError(f'{path}: skew-symmetric matrix requires a signed type')
                dimensions = rows
                continue
            if len(fields) != (2 if kind == 'pattern' else 3):
                raise ValueError(f'{path}:{line_number}: invalid matrix entry')
            try:
                row, col = map(int, fields[:2])
                if not (1 <= row <= rows and 1 <= col <= cols):
                    raise ValueError('matrix index out of bounds')
                if kind != 'pattern':
                    value = fields[2]
                    if value_type in integers:
                        low, high = integers[value_type]
                        if not low <= int(value) <= high:
                            raise ValueError('entry value out of range')
                    else:
                        float(value)
            except ValueError as exc:
                raise ValueError(f'{path}:{line_number}: {exc}') from exc
            count += 1
        if dimensions is None:
            raise ValueError(f'{path}: missing matrix dimensions')
        if count != entries:
            raise ValueError(f'{path}: expected {entries} entries, found {count}')
        return dimensions


def query_metadata(directory):
    path = directory / 'meta.txt'
    tokens = path.read_text().split()
    position = 0

    def integer(signed=False):
        nonlocal position
        if position >= len(tokens):
            raise ValueError(f'{path}: truncated metadata')
        token = tokens[position]
        position += 1
        digits = token[1:] if signed and token.startswith('-') else token
        if not digits.isascii() or not digits.isdecimal():
            raise ValueError(f'{path}: invalid integer {token!r}')
        value = int(token)
        if abs(value) > 2**63 - 1:
            raise ValueError(f'{path}: integer exceeds supported range')
        return value

    source, target = integer(), integer()
    if source and target:
        raise ValueError(f'{path}: fixed source and destination together are unsupported by the API')

    def counted(signed=False, required=False):
        count = integer()
        if count > len(tokens) - position:
            raise ValueError(f'{path}: truncated counted list')
        values = [integer(signed) for _ in range(count)]
        if (required and not values) or any(v == 0 for v in values):
            raise ValueError(f'{path}: expected nonzero IDs and at least one label')
        return values

    initial, final, labels = counted(), counted(), counted(True, required=True)
    if position != len(tokens) or len(set(labels)) != len(labels):
        raise ValueError(f'{path}: trailing metadata or duplicate labels')
    return source, target, initial, final, labels


def validate_queries(queries, matrices):
    # Scan every supplied graph file once, including labels unused by a query:
    # the C driver loads all of them before beginning the benchmark.
    shapes = {label: matrix_shape(path) for label, path in matrices.items()}
    for directory in queries:
        source, target, initial, final, labels = query_metadata(directory)
        graph_sizes, nfa_sizes = set(), set()
        for label in labels:
            key = str(abs(label))
            if key not in matrices:
                raise ValueError(f'{directory}: missing graph matrix for label {key}')
            graph_sizes.add(shapes[key])
            nfa_sizes.add(matrix_shape(directory / f'{label}.txt'))
        if len(graph_sizes) != 1 or len(nfa_sizes) != 1:
            raise ValueError(f'{directory}: inconsistent graph or NFA matrix dimensions')
        if max(source, target) > next(iter(graph_sizes)):
            raise ValueError(f'{directory}: endpoint exceeds graph dimension')
        if max(initial + final, default=0) > next(iter(nfa_sizes)):
            raise ValueError(f'{directory}: NFA state exceeds matrix dimension')


def validate_samples(path):
    """Check measured path counts outside the C driver and timed calls."""
    counts = {}
    with path.open() as stream:
        for line in stream:
            query, count = line.rstrip('\n').rsplit('\t', 1)
            count = int(count)
            if query in counts and counts[query] != count:
                raise ValueError(f'{query}: path count changed between iterations')
            counts[query] = count


def validate_stage_times(path, queries, passes):
    """A successful run must report both stages for every invocation in order."""
    names = [directory.resolve().name for directory in queries]
    with path.open() as stream:
        reader = csv.reader(stream, delimiter='\t')
        if next(reader, None) != ['query', 'matrix_seconds', 'reconstruction_seconds']:
            raise ValueError('invalid stage timing header')
        count = 0
        for row in reader:
            if len(row) != 3 or row[0] != names[count % len(names)]:
                raise ValueError('stage timing query order does not match the batch')
            for field in row[1:]:
                value = float(field)
                if not math.isfinite(value) or value < 0:
                    raise ValueError('stage times must be finite and nonnegative')
            count += 1
        if count != len(names) * passes:
            raise ValueError('missing stage timing rows; rebuild the C driver and LAGraph')
