"""Bounded read-only extraction of one visible XLSX worksheet."""

import csv
from dataclasses import dataclass
import io
import re

from .document_text import DocumentTextError, MAX_TEXT_BYTES, _ooxml_archive, _read_xml_part, _xml_tree


_SHEET_NAMESPACES = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
)
_DOC_REL_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_WORKSHEET_TYPES = {namespace + "/worksheet" for namespace in _DOC_REL_NAMESPACES}
_SHARED_STRING_TYPES = {namespace + "/sharedStrings" for namespace in _DOC_REL_NAMESPACES}
_SHEET_TARGET = re.compile(r"(?:/xl/)?(worksheets/[A-Za-z0-9_.-]+\.xml)")
_STRING_TARGET = re.compile(r"(?:/xl/)?(sharedStrings\.xml)")
_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
_INDEX = re.compile(r"[0-9]{1,7}")
MAX_ROWS, MAX_COLUMNS = 500, 64
_FORMULA_CELL = "[формула без вычисления]"


@dataclass(frozen=True)
class XlsxText:
    source: str
    sheet: str
    rows: tuple[tuple[str, ...], ...]
    text: str
    formula_cells: int


def _part(archive, name):
    return _xml_tree(_read_xml_part(archive, name, kind="XLSX"), kind="XLSX")


def _relationship_map(archive):
    root = _part(archive, "xl/_rels/workbook.xml.rels")
    if root.tag != _PACKAGE_REL + "Relationships":
        raise DocumentTextError("В XLSX повреждены ссылки на листы.")
    links = {}
    for relation in root.findall(_PACKAGE_REL + "Relationship"):
        identifier = relation.get("Id")
        if not identifier or identifier in links:
            raise DocumentTextError("В XLSX повторяются или отсутствуют идентификаторы частей книги.")
        links[identifier] = relation
    return links


def _internal_target(relation, pattern):
    target = pattern.fullmatch(relation.get("Target", ""))
    if relation.get("TargetMode", "Internal") != "Internal" or not target:
        raise DocumentTextError("XLSX может ссылаться только на поддерживаемую внутреннюю XML-часть.")
    return "xl/" + target[1]


def _workbook(archive):
    root = _part(archive, "xl/workbook.xml")
    prefix = next(("{" + ns + "}" for ns in _SHEET_NAMESPACES if root.tag == "{" + ns + "}workbook"), None)
    if prefix is None:
        raise DocumentTextError("В XLSX нет основной структуры SpreadsheetML.")
    links = _relationship_map(archive)
    sheets, identifiers, titles = {}, set(), set()
    for node in root.findall(f"{prefix}sheets/{prefix}sheet"):
        name = node.get("name", "")
        identifier = next((node.get("{" + ns + "}id") for ns in _DOC_REL_NAMESPACES if node.get("{" + ns + "}id")), None)
        relation = links.get(identifier)
        if (not name.strip() or len(name) > 31 or any(ord(char) < 32 for char in name)
                or name.casefold() in titles or identifier in identifiers or relation is None):
            raise DocumentTextError("В XLSX некорректные имена или ссылки на листы.")
        titles.add(name.casefold())
        identifiers.add(identifier)
        if node.get("state", "visible") != "visible" or relation.get("Type") not in _WORKSHEET_TYPES:
            continue
        sheets[name] = _internal_target(relation, _SHEET_TARGET)
    if not sheets:
        raise DocumentTextError("В XLSX нет видимых листов с ячейками.")
    return prefix, sheets, links


def xlsx_sheets(raw):
    """List visible worksheets only, without opening cell data."""
    with _ooxml_archive(raw, kind="XLSX") as archive:
        _, sheets, _ = _workbook(archive)
        return tuple(sheets)


def _string(node, prefix):
    if node is None:
        return ""
    pieces = []
    for child in node:
        if child.tag == prefix + "t":
            pieces.append(child.text or "")
        elif child.tag == prefix + "r":
            pieces.extend(item.text or "" for item in child.findall(prefix + "t"))
    return "".join(pieces)


def _shared_strings(archive, links, prefix):
    matches = [relation for relation in links.values() if relation.get("Type") in _SHARED_STRING_TYPES]
    if len(matches) != 1:
        raise DocumentTextError("В XLSX нужна одна внутренняя таблица общих строк.")
    path = _internal_target(matches[0], _STRING_TARGET)
    root = _part(archive, path)
    if root.tag != prefix + "sst":
        raise DocumentTextError("В XLSX повреждена таблица общих строк.")
    return tuple(_string(node, prefix) for node in root.findall(prefix + "si"))


def _row_number(node, previous):
    raw = node.get("r")
    if raw is not None and not _INDEX.fullmatch(raw):
        raise DocumentTextError("В XLSX некорректный номер строки.")
    number = previous + 1 if raw is None else int(raw)
    if number <= previous:
        raise DocumentTextError("В XLSX повторяются или перепутаны строки.")
    return number


def _column_number(node, row, previous):
    reference = node.get("r")
    if reference is None:
        return previous + 1
    match = _ADDRESS.fullmatch(reference)
    if not match or int(match[2]) != row:
        raise DocumentTextError("В XLSX некорректная ссылка на ячейку.")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    if column <= previous:
        raise DocumentTextError("В XLSX повторяются или перепутаны ячейки.")
    return column


def _value(cell, prefix, shared):
    kind = cell.get("t", "n")
    raw = cell.findtext(prefix + "v", "")
    if kind == "s":
        if not _INDEX.fullmatch(raw) or int(raw) >= len(shared):
            raise DocumentTextError("Ячейка XLSX ссылается на отсутствующую общую строку.")
        return shared[int(raw)]
    if kind == "inlineStr":
        return _string(cell.find(prefix + "is"), prefix)
    if kind == "b":
        if raw not in {"0", "1"}:
            raise DocumentTextError("В XLSX некорректное логическое значение.")
        return "TRUE" if raw == "1" else "FALSE"
    if kind in {"n", "str", "e", "d"}:
        return raw
    raise DocumentTextError("В XLSX встретился неподдерживаемый тип ячейки.")


def _grid(archive, root, links, prefix):
    sheet_data = root.find(prefix + "sheetData") if root.tag == prefix + "worksheet" else None
    if sheet_data is None:
        raise DocumentTextError("В XLSX повреждена структура выбранного листа.")
    shared = None
    grid, width, size, formulas = [], 0, 0, 0
    for node in sheet_data.findall(prefix + "row"):
        row = _row_number(node, len(grid))
        if row > MAX_ROWS:
            raise DocumentTextError(f"Лист XLSX превышает лимит {MAX_ROWS} строк. Сохрани меньший диапазон отдельно.")
        grid.extend([] for _ in range(row - len(grid)))
        line = grid[-1]
        for cell in node.findall(prefix + "c"):
            column = _column_number(cell, row, len(line))
            if column > MAX_COLUMNS:
                raise DocumentTextError(f"Лист XLSX превышает лимит {MAX_COLUMNS} столбцов. Сохрани меньший диапазон отдельно.")
            if cell.find(prefix + "f") is not None:
                value = _FORMULA_CELL
                formulas += 1
            else:
                if cell.get("t") == "s" and shared is None:
                    shared = _shared_strings(archive, links, prefix)
                value = _value(cell, prefix, shared)
            size += len(value.encode())
            if size > MAX_TEXT_BYTES:
                raise DocumentTextError("Значения ячеек XLSX превышают лимит 256 КиБ.")
            line.extend([""] * (column - len(line) - 1))
            line.append(value)
        width = max(width, len(line))
    return tuple(tuple(row + [""] * (width - len(row))) for row in grid) if width else (), formulas


def extract_xlsx(raw, *, source, sheet=None):
    """Extract a selected worksheet as a bounded literal grid and quoted TSV.

    Styles, serial-date interpretation, drawings and formulas are outside the
    extraction path. Formula expressions and cached results are never returned.
    """
    with _ooxml_archive(raw, kind="XLSX") as archive:
        prefix, sheets, links = _workbook(archive)
        names = ", ".join(f"«{name}»" for name in tuple(sheets)[:12])
        if len(sheets) > 12:
            names += " и другие"
        if sheet is None and len(sheets) == 1:
            sheet = next(iter(sheets))
        if not isinstance(sheet, str) or sheet not in sheets:
            raise DocumentTextError(f"Укажи точное имя видимого листа XLSX. Листы книги — {names}.")
        rows, formulas = _grid(archive, _part(archive, sheets[sheet]), links, prefix)
    output = io.StringIO(newline="")
    csv.writer(output, dialect="excel-tab").writerows(rows)
    text = output.getvalue().removesuffix("\r\n")
    if len(text.encode()) > MAX_TEXT_BYTES:
        raise DocumentTextError("Извлечённая таблица XLSX превышает лимит 256 КиБ.")
    return XlsxText(str(source), sheet, rows, text, formulas)
