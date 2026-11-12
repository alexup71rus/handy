"""Validate bounded semantic conditions and apply them to observed table cells."""

import json
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .table_analysis import parse_numeric


_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


def _sequence(value, limit, label):
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError('Invalid ' + label)
    return value


def _source(value):
    if not isinstance(value, dict) or set(value) != {'start', 'end', 'text'}:
        raise ValueError('Invalid condition source')
    start, end, text = value['start'], value['end'], value['text']
    if (type(start) is not int or type(end) is not int or start < 0
            or not isinstance(text, str) or not text or end - start != len(text)
            or end > 16000):
        raise ValueError('Invalid condition source span')


def _number(value):
    if type(value) not in {str, int, float}:
        raise ValueError('Invalid numeric condition literal')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Numeric condition must be finite')
    text = str(value)
    if len(text) > 512 or not _NUMBER.fullmatch(text):
        raise ValueError('Invalid numeric condition literal')
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid numeric condition literal') from None
    if not number.is_finite():
        raise ValueError('Numeric condition must be finite')
    return number


def validate_constraints(value):
    allowed = {'include_extensions', 'exclude_extensions', 'exclude_kinds', 'where', 'unresolved',
               'coverage', 'requires_clarification', 'kind'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Invalid request constraints')
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError('Request constraints must contain finite JSON data') from None
    if len(encoded) > 12000:
        raise ValueError('Request constraints are too large')
    if value.get('kind') is not None and value['kind'] not in ('file', 'directory'):
        raise ValueError('Invalid requested entry kind')
    for kind in _sequence(value.get('exclude_kinds', []), 2, 'excluded entry kinds'):
        if kind not in ('file', 'directory'):
            raise ValueError('Invalid excluded entry kind')
    if 'requires_clarification' in value and type(value['requires_clarification']) is not bool:
        raise ValueError('Invalid clarification flag')
    for key in ('include_extensions', 'exclude_extensions'):
        for extension in _sequence(value.get(key, []), 16, 'extension constraints'):
            if not isinstance(extension, str) or not extension.isalnum() or len(extension) > 16:
                raise ValueError('Invalid extension constraint')
    for clause in _sequence(value.get('where', []), 4, 'table conditions'):
        allowed_clause = {'column', 'operator', 'value', 'value_type', 'raw_value', 'source'}
        if (not isinstance(clause, dict) or set(clause) - allowed_clause
                or not isinstance(clause.get('column'), str) or not clause['column'].strip()
                or len(clause['column']) > 160 or clause.get('operator') not in ('eq', 'ne')
                or 'value' not in clause):
            raise ValueError('Invalid table condition')
        kind, item = clause.get('value_type', 'string'), clause['value']
        if kind == 'number':
            expected = _number(item)
        elif kind == 'string':
            if not isinstance(item, str) or len(item) > 512:
                raise ValueError('Invalid string condition literal')
        elif kind == 'boolean':
            if type(item) is not bool:
                raise ValueError('Invalid boolean condition literal')
        elif kind == 'null':
            if item is not None:
                raise ValueError('Invalid null condition literal')
        else:
            raise ValueError('Invalid condition type')
        if 'raw_value' in clause:
            raw = clause['raw_value']
            if not isinstance(raw, str) or len(raw) > 512:
                raise ValueError('Invalid raw condition literal')
            if kind == 'number' and raw and _number(raw) != expected:
                raise ValueError('Numeric condition and its original literal disagree')
        if 'source' in clause:
            _source(clause['source'])
    for key, limit, fields in [('unresolved', 16, {'kind', 'reason', 'source'}),
                               ('coverage', 32, {'operator', 'resolved', 'source'})]:
        for item in _sequence(value.get(key, []), limit, key):
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError('Invalid ' + key + ' record')
            for field in fields - {'source', 'resolved'}:
                if not isinstance(item[field], str) or not 0 < len(item[field]) <= 160:
                    raise ValueError('Invalid ' + key + ' record')
            if key == 'coverage' and type(item['resolved']) is not bool:
                raise ValueError('Invalid coverage resolution flag')
            _source(item['source'])
    return value


def _fold(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def _numeric_cell(cell):
    # JSON cells and unambiguous decimal/exponent lexemes keep exact precision.
    if type(cell) in {int, float, str}:
        try:
            return _number(cell)
        except ValueError:
            pass
    # Preserve support for observed localized amounts, e.g. "1 234,50 руб.".
    parsed = parse_numeric(cell)
    return parsed[0] if parsed is not None else None


def _table_shape(table):
    if not isinstance(table, dict) or not isinstance(table.get('headers'), (list, tuple)) or not isinstance(table.get('rows'), (list, tuple)):
        raise ValueError('Invalid observed table')
    headers, rows = table['headers'], table['rows']
    if any(not isinstance(name, str) for name in headers) or any(not isinstance(row, (list, tuple)) for row in rows):
        raise ValueError('Invalid observed table shape')
    types = table.get('cell_types')
    if 'cell_types' in table:
        if not isinstance(types, (list, tuple)) or len(types) != len(rows):
            raise ValueError('Invalid cell type sidecar')
        for row, tags in zip(rows, types):
            if (not isinstance(tags, (list, tuple)) or len(tags) != len(row)
                    or any(tag not in ('string', 'number', 'boolean', 'null') for tag in tags)):
                raise ValueError('Cell types must align with observed rows')
    return headers, rows, types


def _typed_cell(cell, tag=None):
    if tag is None:
        tag = ('null' if cell is None else 'boolean' if type(cell) is bool
               else 'number' if type(cell) in {int, float} else 'string')
    if tag == 'null' and cell is None:
        return None, tag
    if tag == 'string' and isinstance(cell, str):
        return cell, tag
    if tag == 'number':
        _number(cell)
        return str(cell), tag
    if tag == 'boolean':
        if type(cell) is bool:
            return cell, tag
        if isinstance(cell, str) and cell in ('true', 'false'):
            return cell == 'true', tag
    raise ValueError('Observed cell does not agree with its scalar type')


def condition_cell(table, row_index, column_index):
    """Return a native choice's literal and type, preserving JSON numeric lexemes.
    CSV/DOM text stays string; JSON boolean/null cells recover their source type.
    """
    headers, rows, types = _table_shape(table)
    if (type(row_index) is not int or type(column_index) is not int
            or not 0 <= row_index < len(rows) or not 0 <= column_index < len(headers)):
        raise ValueError('Observed cell index is out of range')
    row = rows[row_index]
    cell = row[column_index] if column_index < len(row) else None
    tag = types[row_index][column_index] if types is not None and column_index < len(row) else None
    return _typed_cell(cell, tag)


def filter_table(table, constraints):
    validate_constraints(constraints)
    if constraints.get('unresolved') or constraints.get('requires_clarification'):
        raise ValueError('Условие ещё не уточнено.')
    headers, source_rows, types = _table_shape(table)
    source_indices = table.get('source_row_indices')
    if source_indices is not None and (not isinstance(source_indices, (list, tuple))
                                       or len(source_indices) != len(source_rows)):
        raise ValueError('Source row indices must align with observed rows')
    columns = {}
    for index, name in enumerate(headers):
        columns.setdefault(_fold(name), []).append(index)
    predicates = []
    for clause in constraints.get('where', []):
        indices = columns.get(_fold(clause['column']), [])
        if not indices:
            raise ValueError('В файле нет столбца ' + clause['column'] + '.')
        if len(indices) != 1:
            raise ValueError('Столбец ' + clause['column'] + ' встречается неоднозначно.')
        expected = _number(clause.get('raw_value') or clause['value']) if clause.get('value_type') == 'number' else clause['value']
        predicates.append((indices[0], clause.get('value_type', 'string'), clause['operator'], expected))

    def matches(row_index, row):
        for index, kind, operator, expected in predicates:
            cell = row[index] if index < len(row) else None
            actual_kind = None
            if types is not None:
                tag = types[row_index][index] if index < len(row) else 'null'
                cell, actual_kind = _typed_cell(cell, tag)
            if actual_kind is not None and actual_kind != kind:
                equal = False
            elif kind == 'number':
                equal = _numeric_cell(cell) == expected
            elif kind == 'null':
                equal = cell is None
            elif kind == 'boolean':
                equal = cell is expected or isinstance(cell, str) and _fold(cell) == str(expected).lower()
            else:
                equal = isinstance(cell, str) and _fold(cell) == _fold(expected)
            if equal != (operator == 'eq'):
                return False
        return True

    selected = [index for index, row in enumerate(source_rows) if matches(index, row)]
    result = {**table, 'rows': [source_rows[index] for index in selected], 'total_rows': len(selected),
              'source_row_indices': selected if source_indices is None else [source_indices[index] for index in selected],
              'source_total_rows': table.get('source_total_rows', table.get('total_rows', len(source_rows))),
              'conditions': list(constraints.get('where', [])),
              'observed_rows_before_filter': len(source_rows)}
    if types is not None:
        result['cell_types'] = [types[index] for index in selected]
    return result
