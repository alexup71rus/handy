"""Parse bounded source tables without generating values or running commands."""

import csv
import io
import json
import unicodedata


MAX_INPUT_BYTES = 256 * 1024
MAX_ROWS, MAX_COLUMNS, MAX_JSON_DEPTH = 100, 20, 32
_COLLECTION_FIELDS = {'data', 'items', 'records', 'results', 'rows'}


class JSONEnvelopeError(ValueError):
    pass


class _Number(str):
    pass


def _headers(values):
    if not values or any(not isinstance(value, str) or isinstance(value, _Number) or not value.strip() or any(ord(char) < 32 for char in value) for value in values):
        raise ValueError("Table requires nonempty explicit string headers")
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Table header contains invalid Unicode") from None
    names = [unicodedata.normalize("NFKC", value).strip().casefold() for value in values]
    if len(names) != len(set(names)):
        raise ValueError("Table headers must be unique")
    return list(values)


def _cell(value):
    if isinstance(value, (dict, list)):
        raise ValueError("Nested JSON cells are not supported")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Table cell contains invalid Unicode") from None
    return value


def _cell_type(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, _Number):
        return 'number'
    if isinstance(value, str):
        return 'string'
    raise ValueError('JSON table cells must be scalars')


def _wrapped_rows(value):
    scopes = [((), value)]
    if isinstance(value.get('data'), dict):
        scopes.append((('data',), value['data']))
    candidates = [(prefix + (key,), rows, scope) for prefix, scope in scopes for key, rows in scope.items()
                  if key in _COLLECTION_FIELDS and isinstance(rows, list)]
    if not candidates:
        return value, None
    if len(candidates) != 1:
        raise JSONEnvelopeError('В ответе несколько массивов строк. Нужен ответ с одним набором данных.')
    path, rows, owner = candidates[0]
    row_count = len(rows) - 1 if rows and isinstance(rows[0], list) else len(rows)
    containers = (value,) if owner is value else (value, owner)
    metadata = [scope for container in containers for scope in
                (container, container.get('pagination'), container.get('meta'), container.get('links'))
                if isinstance(scope, dict)]
    for scope in metadata:
        more = (scope.get('has_more') is True or scope.get('hasMore') is True or scope.get('hasNextPage') is True
                or any(scope.get(key) for key in ('next', 'next_cursor', 'next_page')))
        totals = (scope.get(key) for key in ('total', 'total_count', 'totalCount'))
        if more or any(isinstance(total, str) and total.isascii() and total.isdecimal()
                       and (len(total) > 20 or int(total) > row_count) for total in totals):
            raise JSONEnvelopeError('Ответ содержит только страницу данных. Для расчёта нужен полный массив.')
    return rows, list(path)


def _json(text, max_rows, max_columns, *, json_envelope):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON exceeds the nesting budget")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Nonfinite JSON numbers are not supported")

    # Preserve numeric lexemes: 0.10 must not become a binary float artifact.
    value = json.loads(text, object_pairs_hook=pairs, parse_int=_Number, parse_float=_Number, parse_constant=invalid_constant)
    path = None
    if json_envelope and isinstance(value, dict):
        value, path = _wrapped_rows(value)
    if not isinstance(value, list) or not value:
        raise ValueError("JSON table must be a nonempty array")
    if all(isinstance(row, dict) for row in value):
        headers = _headers(list(dict.fromkeys(key for row in value for key in row)))
        for row in value:
            for cell in row.values():
                _cell(cell)
        visible = [[row.get(header) for header in headers[:max_columns]] for row in value[:max_rows]]
        rows = [[_cell(cell) for cell in row] for row in visible]
        cell_types = [[_cell_type(cell) for cell in row] for row in visible]
        return headers, rows, "object_keys", len(value), cell_types, path
    if all(isinstance(row, list) for row in value):
        headers = _headers(value[0])
        rows = value[1:]
        if any(len(row) != len(headers) for row in rows):
            raise ValueError("Table row width does not match its headers")
        for row in rows:
            for cell in row:
                _cell(cell)
        visible = [row[:max_columns] for row in rows[:max_rows]]
        return (headers, [[_cell(cell) for cell in row] for row in visible], "first_row", len(rows),
                [[_cell_type(cell) for cell in row] for row in visible], path)
    raise ValueError("JSON table rows must be uniformly objects or arrays")


def parse_table(text, suffix, *, max_rows=MAX_ROWS, max_columns=MAX_COLUMNS, json_envelope=False):
    """Return aligned literal cells for analyze_table, with JSON scalar type tags.
    Absent object keys remain null; limits never shift columns.
    """
    if not isinstance(text, str) or not isinstance(suffix, str):
        raise ValueError("Table input and suffix must be strings")
    try:
        if len(text) > MAX_INPUT_BYTES or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("Table input exceeds 256 KiB")
    except UnicodeEncodeError:
        raise ValueError("Table input contains invalid Unicode") from None
    kind = suffix.lower().removeprefix(".")
    text = text.removeprefix("\ufeff")
    cell_types = None
    path = None
    try:
        if kind == "json":
            headers, rows, source, total_rows, cell_types, path = _json(
                text, max_rows, max_columns, json_envelope=json_envelope,
            )
        elif kind in {"csv", "tsv", "xlsx"}:
            records = [row for row in csv.reader(io.StringIO(text, newline=""), delimiter="," if kind == "csv" else "\t", strict=True) if row]
            if not records:
                raise ValueError("Table requires explicit headers")
            headers, rows, source = _headers(records[0]), records[1:], "first_row"
            if any(len(row) != len(headers) for row in rows):
                raise ValueError("Table row width does not match its headers")
            total_rows = len(rows)
        else:
            raise ValueError("Only CSV, TSV, JSON and XLSX tables are supported")
    except (csv.Error, json.JSONDecodeError, RecursionError):
        raise ValueError("Malformed or oversized table input") from None
    result = {"headers": headers[:max_columns], "rows": [row[:max_columns] for row in rows[:max_rows]],
              "partial": total_rows > max_rows or len(headers) > max_columns,
              "total_rows": total_rows, "total_columns": len(headers), "format": kind, "header_source": source}
    if cell_types is not None:
        result['cell_types'] = cell_types
    if path is not None:
        result['json_path'] = path
    return result
