"""Validation for the original Wikidata Matrix Market query decomposition."""
from pathlib import Path


def matrix_shape(path):
    with Path(path).open() as stream:
        banner = stream.readline().lower().split()
        if (len(banner) != 5 or banner[:3] != ["%%matrixmarket", "matrix", "coordinate"]
                or banner[3] not in ("pattern", "integer", "real", "complex")
                or banner[4] not in ("general", "symmetric", "skew-symmetric", "hermitian")):
            raise ValueError(f"{path}: expected a Matrix Market coordinate matrix")
        for line in stream:
            if line.strip() and not line.lstrip().startswith('%'):
                values = line.split()
                if len(values) != 3 or any(not v.isdecimal() for v in values):
                    raise ValueError(f"{path}: invalid matrix dimensions")
                rows, cols, _ = map(int, values)
                if rows != cols:
                    raise ValueError(f"{path}: expected a square matrix")
                return rows
    raise ValueError(f"{path}: missing matrix dimensions")


def query_metadata(directory):
    path = directory / "meta.txt"
    tokens = path.read_text().split()
    position = 0

    def integer(signed=False):
        nonlocal position
        if position >= len(tokens):
            raise ValueError(f"{path}: truncated metadata")
        token = tokens[position]
        position += 1
        digits = token[1:] if signed and token.startswith('-') else token
        if not digits.isascii() or not digits.isdecimal():
            raise ValueError(f"{path}: invalid integer {token!r}")
        value = int(token)
        if abs(value) > 2**63 - 1:
            raise ValueError(f"{path}: integer exceeds supported range")
        return value

    source, target = integer(), integer()
    def counted(signed=False):
        count = integer()
        if count > len(tokens) - position:
            raise ValueError(f"{path}: truncated counted list")
        values = [integer(signed) for _ in range(count)]
        if not values or any(v == 0 for v in values):
            raise ValueError(f"{path}: expected a nonempty list of nonzero IDs")
        return values
    initial, final, labels = counted(), counted(), counted(True)
    if position != len(tokens) or len(set(labels)) != len(labels):
        raise ValueError(f"{path}: trailing metadata or duplicate labels")
    return source, target, initial, final, labels


def validate_queries(queries, matrices):
    shapes = {}
    for directory in queries:
        source, target, initial, final, labels = query_metadata(directory)
        graph_sizes, nfa_sizes = set(), set()
        for label in labels:
            key = str(abs(label))
            if key not in matrices:
                raise ValueError(f"{directory}: missing graph matrix for label {key}")
            if key not in shapes:
                shapes[key] = matrix_shape(matrices[key])
            graph_sizes.add(shapes[key])
            nfa_sizes.add(matrix_shape(directory / f"{label}.txt"))
        if len(graph_sizes) != 1 or len(nfa_sizes) != 1:
            raise ValueError(f"{directory}: inconsistent graph or NFA matrix dimensions")
        if max(source, target) > next(iter(graph_sizes)):
            raise ValueError(f"{directory}: endpoint exceeds graph dimension")
        if max(initial + final) > next(iter(nfa_sizes)):
            raise ValueError(f"{directory}: NFA state exceeds matrix dimension")
