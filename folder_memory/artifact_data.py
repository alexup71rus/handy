"""Turn literal observations into the template data contracts."""

from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .structured_data import parse_table
from .table_analysis import parse_numeric


CHAT_ROWS = 20
MAX_ROWS = 5000
MAX_COLUMNS = 16
MAX_POINTS = 366
MAX_DONUT_POINTS = 12
_PLAIN_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:[.,][0-9]+)?")


def read_display_table(text, suffix, *, json_envelope=False):
    table = parse_table(text, suffix, max_rows=MAX_ROWS, max_columns=MAX_COLUMNS, json_envelope=json_envelope)
    if table["partial"]:
        raise ValueError(f"Для показа доступны таблицы до {MAX_ROWS} строк и {MAX_COLUMNS} столбцов. Выбери меньший набор данных.")
    return table


def prefer_page(table):
    rows = table["rows"]
    return len(rows) > CHAT_ROWS or sum(len(str(cell)) for row in rows for cell in row if cell is not None) > 4000


def _exact_float(number):
    if not number.is_finite() or number.copy_abs() > 10**16:
        return None
    value = float(number)
    return value if Decimal(str(value)) == number else None


def _numeric_column(values, types):
    numbers = []
    for value, tag in zip(values, types):
        if value is None or value == "":
            numbers.append(None)
            continue
        if tag != "number" and not (tag is None and _PLAIN_NUMBER.fullmatch(str(value))):
            return None
        try:
            number = Decimal(str(value).replace(",", "."))
        except InvalidOperation:
            return None
        if (number := _exact_float(number)) is None:
            return None
        numbers.append(number)
    return numbers if any(value is not None for value in numbers) else None


def _is_date(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def table_page(table, title, description=""):
    columns, values = [], []
    rows = table["rows"]
    types = table.get("cell_types")
    for index, label in enumerate(table["headers"]):
        column = [row[index] if index < len(row) else None for row in rows]
        tags = [row[index] for row in types] if types is not None else [None] * len(rows)
        numeric = _numeric_column(column, tags)
        kind = "text"
        if numeric is not None:
            kind, column = "number", numeric
        elif any(column) and all(value in (None, "") or _is_date(value) for value in column):
            kind, column = "date", [value or None for value in column]
        else:
            column = [None if value is None else str(value) for value in column]
        columns.append({"key": f"c{index}", "label": label, "type": kind})
        values.append(column)
    return {"title": title, "description": description, "columns": columns,
            "rows": [dict(zip((column["key"] for column in columns), row)) for row in zip(*values)]}


def _fold(value):
    return unicodedata.normalize("NFKC", value).strip().casefold().replace("ё", "е")


def _axis(headers, name):
    if name is None:
        return None
    choices = [index for index, header in enumerate(headers) if _fold(header) == _fold(name)]
    if len(choices) != 1:
        raise ValueError(f"Столбец «{name}» не определён однозначно. Доступны {', '.join(headers)}.")
    return choices[0]


def _series(table, index):
    points, units = [], set()
    types = table.get("cell_types")
    for i, row in enumerate(table["rows"]):
        value = row[index] if index < len(row) else None
        try:
            parsed = (Decimal(value), None) if types and types[i][index] == "number" else parse_numeric(value)
        except InvalidOperation:
            return None
        if parsed is None:
            return None
        number, unit = parsed
        exact = _exact_float(number)
        if exact is None or abs(exact) > 10**12:
            return None
        units.add(unit)
        points.append(exact)
    return (points, next(iter(units))) if len(units) == 1 else None


def chart_page(table, title, x=None, y=None, description="", *, chart_type="line"):
    rows, headers = table["rows"], table["headers"]
    if not 1 <= len(rows) <= MAX_POINTS:
        raise ValueError(f"Для графика нужен набор от 1 до {MAX_POINTS} точек. Уточни диапазон данных.")
    x_index, y_index = _axis(headers, x), _axis(headers, y)
    if y_index is None:
        options = [index for index in range(len(headers)) if index != x_index and _series(table, index) is not None]
        if len(options) != 1:
            raise ValueError(f"Укажи числовой столбец для Y. Столбцы в источнике — {', '.join(headers)}.")
        y_index = options[0]
    if x_index is None and len(headers) == 2:
        x_index = 1 - y_index
    if x_index == y_index or x_index is None and len(headers) > 1:
        raise ValueError(f"Укажи отдельный столбец для X. Столбцы в источнике — {', '.join(headers)}.")
    series = _series(table, y_index)
    if series is None:
        raise ValueError(f"Столбец «{headers[y_index]}» должен содержать числа без пропусков и с одинаковыми единицами.")
    values, unit = series
    if chart_type == "donut":
        if len(values) > MAX_DONUT_POINTS:
            raise ValueError(f"Для кольцевой диаграммы выбери не больше {MAX_DONUT_POINTS} категорий.")
        if any(value < 0 for value in values) or sum(values) <= 0:
            raise ValueError("Для кольцевой диаграммы нужны неотрицательные значения с суммой больше нуля. Для остальных данных подойдёт линия или столбцы.")
    labels = [str(i) for i in range(1, len(rows) + 1)] if x_index is None else [row[x_index] for row in rows]
    if any(value is None or not str(value).strip() for value in labels):
        raise ValueError("В столбце X есть пустые подписи. Выбери другой столбец или отфильтруй строки.")
    return {"title": title, "description": description, "label": headers[y_index], "chartType": chart_type,
            **({"unit": unit} if unit else {}),
            "points": [{"label": str(label), "value": value} for label, value in zip(labels, values)]}


def list_page(raw, suffix, title):
    if suffix == "json":
        try:
            items = json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise ValueError("Для списка нужен JSON-массив пунктов.") from error
    elif suffix in (None, "list", "text"):
        items = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            label = re.sub(r"^\s*(?:[-*•]|[0-9]+[.)])\s+", "", line).strip()
            checked = re.fullmatch(r"\[([ xX])\]\s+(.+)", label)
            items.append({"label": checked[2], "checked": checked[1].lower() == "x"} if checked else label)
    else:
        raise ValueError("Для списка пришли пункты или JSON-массив.")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ROWS:
        raise ValueError(f"Для списка нужен массив или от 1 до {MAX_ROWS} пунктов.")
    for item in items:
        if isinstance(item, str) and item.strip():
            continue
        if (not isinstance(item, dict) or set(item) - {"label", "detail", "checked"}
                or not isinstance(item.get("label"), str) or not item["label"].strip()
                or not isinstance(item.get("detail", ""), str) or type(item.get("checked", False)) is not bool):
            raise ValueError("Пункт списка должен быть строкой или объектом с label, detail и checked.")
    return {"title": title, "items": items, "checklist": any(isinstance(item, dict) and "checked" in item for item in items)}


def clock_page(title, time_zone):
    if time_zone:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Укажи часовой пояс IANA, например Europe/Moscow или UTC.") from error
    return {"title": title, **({"timeZone": time_zone} if time_zone else {})}
