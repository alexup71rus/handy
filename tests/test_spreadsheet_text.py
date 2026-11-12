import csv
import io
import unittest
from unittest.mock import patch
from xml.sax.saxutils import quoteattr
import zipfile

from folder_memory.document_text import DocumentTextError, MAX_ARCHIVE_BYTES, MAX_UNCOMPRESSED_BYTES
from folder_memory.spreadsheet_text import MAX_ROWS, extract_xlsx, xlsx_sheets


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def grid(rows):
    return f'<worksheet xmlns="{NS}"><sheetData>{rows}</sheetData></worksheet>'


def workbook(sheets, *, shared=None, extra=(), target=None):
    book_sheets, links, files = [], [], []
    for index, (name, body, state) in enumerate(sheets, 1):
        identifier = f"rId{index}"
        book_sheets.append(f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="{identifier}" state="{state}"/>')
        path = f"xl/worksheets/sheet{index}.xml"
        url = target if target is not None and index == 1 else "/" + path
        links.append(f'<Relationship Id="{identifier}" Type="{DOC_REL}/worksheet" Target={quoteattr(url)}/>')
        files.append((path, body))
    if shared is not None:
        links.append(f'<Relationship Id="rIdStrings" Type="{DOC_REL}/sharedStrings" Target="sharedStrings.xml"/>')
        files.append(("xl/sharedStrings.xml", f'<sst xmlns="{NS}">{shared}</sst>'))
    book = f'<workbook xmlns="{NS}" xmlns:r="{DOC_REL}"><sheets>{"".join(book_sheets)}</sheets></workbook>'
    rels = f'<Relationships xmlns="{PACKAGE_REL}">{"".join(links)}</Relationships>'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in [("xl/workbook.xml", book), ("xl/_rels/workbook.xml.rels", rels), *files, *extra]:
            archive.writestr(name, data)
    return stream.getvalue()


class SpreadsheetTextTests(unittest.TestCase):
    def test_literal_values_and_sparse_coordinates_roundtrip_as_quoted_tsv(self):
        rows = (
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is>'
            '<t>Цена&#9;RUB&#10;партия</t></is></c></row>'
            '<row r="3"><c r="A3" t="b"><v>1</v></c><c r="C3"><v>25.50</v></c></row>'
        )
        shared = '<si><r><t>Бренд</t></r><r><t xml:space="preserve"> &amp; модель</t></r><rPh><t>фонетика</t></rPh></si>'
        raw = workbook([("Прайс", grid(rows), "visible")], shared=shared)
        result = extract_xlsx(raw, source="prices.xlsx")
        expected = (("Бренд & модель", "", "Цена\tRUB\nпартия"), ("", "", ""), ("TRUE", "", "25.50"))
        self.assertEqual(result.rows, expected)
        self.assertEqual(tuple(tuple(row) for row in csv.reader(io.StringIO(result.text), dialect="excel-tab")), expected)
        self.assertEqual((result.source, result.sheet, result.formula_cells), ("prices.xlsx", "Прайс", 0))

    def test_formula_and_cached_result_are_omitted(self):
        formula = '<c r="A1" t="str"><f>WEBSERVICE("https://example.invalid/")</f><v>untrusted cache</v></c>'
        raw = workbook([("Лист", grid(f'<row r="1">{formula}</row>'), "visible")])
        result = extract_xlsx(raw, source="formula.xlsx")
        self.assertEqual(result.formula_cells, 1)
        self.assertIn("формула", result.text)
        self.assertNotIn("untrusted cache", result.text)
        self.assertNotIn("WEBSERVICE", result.text)

    def test_selects_only_unique_or_explicit_visible_sheet(self):
        first = grid('<row><c t="inlineStr"><is><t>Один</t></is></c></row>')
        second = grid('<row><c t="inlineStr"><is><t>Два</t></is></c></row>')
        raw = workbook([("Первый", first, "visible"), ("Второй", second, "visible"), ("Скрытый", b"not XML", "veryHidden")])
        self.assertEqual(xlsx_sheets(raw), ("Первый", "Второй"))
        with self.assertRaisesRegex(DocumentTextError, "Первый.*Второй"):
            extract_xlsx(raw, source="book.xlsx")
        for sheet in ("Скрытый", "второй", "Нет"):
            with self.subTest(sheet=sheet), self.assertRaises(DocumentTextError):
                extract_xlsx(raw, source="book.xlsx", sheet=sheet)
        original = zipfile.ZipFile.open
        opened = []

        def record(archive, member, *args, **kwargs):
            opened.append(member.filename)
            return original(archive, member, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", record):
            self.assertEqual(extract_xlsx(raw, source="book.xlsx", sheet="Второй").text, "Два")
        self.assertEqual(opened, ["xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet2.xml"])
        one_visible = workbook([("Скрытый", b"not XML", "hidden"), ("Второй", second, "visible")])
        self.assertEqual(extract_xlsx(one_visible, source="book.xlsx").text, "Два")

    def test_dtd_remote_relationships_and_archive_limits_are_refused(self):
        dtd = '<!DOCTYPE worksheet [<!ENTITY x SYSTEM "file:///should-not-be-read">]>'
        sheet = grid('<row><c t="inlineStr"><is><t>&x;</t></is></c></row>')
        for raw in [
            b"not a ZIP",
            b"x" * (MAX_ARCHIVE_BYTES + 1),
            workbook([("Sheet", grid(""), "visible")], target="https://example.invalid/sheet.xml"),
            workbook([("Sheet", grid(""), "visible")], target="../worksheets/sheet.xml"),
            workbook([("Sheet", dtd + sheet, "visible")]),
            workbook([("Sheet", grid(""), "visible")], extra=[("../escape.xml", b"blocked")]),
            workbook([("Sheet", grid(""), "visible")], extra=[("xl/bomb.bin", b"x" * (MAX_UNCOMPRESSED_BYTES + 1))]),
        ]:
            with self.subTest(raw_size=len(raw)), self.assertRaises(DocumentTextError):
                extract_xlsx(raw, source="bad.xlsx")

    def test_outside_grid_or_invalid_references_fail_instead_of_truncating(self):
        for rows in [
            f'<row r="{MAX_ROWS + 1}"/>',
            '<row r="1"><c r="BM1"><v>20</v></c></row>',
            '<row r="1"><c r="A2"><v>20</v></c></row>',
            '<row r="1"><c r="A1"><v>20</v></c><c r="A1"><v>30</v></c></row>',
            '<row r="1"><c r="A1" t="s"><v>2000</v></c></row>',
        ]:
            raw = workbook([("Sheet", grid(rows), "visible")], shared='<si><t>only one</t></si>')
            with self.subTest(rows=rows), self.assertRaises(DocumentTextError):
                extract_xlsx(raw, source="bad.xlsx")


if __name__ == "__main__":
    unittest.main()
