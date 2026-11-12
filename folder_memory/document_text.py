"""Bounded, read-only text extraction from the main body of DOCX files."""

from contextlib import contextmanager
from dataclasses import dataclass
import io
from pathlib import PurePosixPath
import stat
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile
import zlib


MAX_ARCHIVE_BYTES = 256 * 1024
MAX_MEMBERS = 256
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_XML_DEPTH = 128
MAX_XML_NODES = 50000
WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}


class DocumentTextError(ValueError):
    pass


@dataclass(frozen=True)
class DocxText:
    source: str
    text: str
    paragraph_count: int
    table_count: int


@contextmanager
def _ooxml_archive(raw, *, kind):
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise DocumentTextError(f"{kind} больше 256 КиБ; чтение отклонено.")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise DocumentTextError(f"В {kind} слишком много частей.")
            names, total = set(), 0
            for member in members:
                name = member.filename
                original = getattr(member, "orig_filename", name)
                if (name in names or "\0" in original or "\\" in name or
                        PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or
                        stat.S_ISLNK(member.external_attr >> 16)):
                    raise DocumentTextError(f"Некорректная структура ZIP внутри {kind}.")
                names.add(name)
                if member.flag_bits & 1:
                    raise DocumentTextError(f"Зашифрованные части {kind} не поддерживаются.")
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise DocumentTextError(f"Этот способ сжатия {kind} не поддерживается.")
                total += member.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise DocumentTextError(f"{kind} превышает лимит распакованных данных.")
            yield archive
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, UnicodeError, zlib.error):
        raise DocumentTextError(f"Не удалось прочитать ZIP-структуру {kind}.") from None


def _read_xml_part(archive, name, *, kind, label=None):
    try:
        member = archive.getinfo(name)
    except KeyError:
        raise DocumentTextError(f"В {kind} нет основной части {name}.") from None
    limit_error = f"{label or 'Часть ' + kind + ' ' + name} превышает лимит 2 МиБ."
    if member.file_size > MAX_XML_BYTES:
        raise DocumentTextError(limit_error)
    # Read a named internal part in memory. Never extract the archive.
    with archive.open(member) as stream:
        xml = stream.read(MAX_XML_BYTES + 1)
    if len(xml) > MAX_XML_BYTES:
        raise DocumentTextError(limit_error)
    return xml


def _main_xml(raw):
    with _ooxml_archive(raw, kind="DOCX") as archive:
        return _read_xml_part(archive, "word/document.xml", kind="DOCX", label="Основной XML DOCX")


def _xml_tree(xml, *, kind="DOCX"):
    # Expat handlers reject a DTD before any entity expansion, including UTF-16
    # XML. Built-in XML escapes such as &amp; remain normal character data.
    parser = expat.ParserCreate(namespace_separator="}")
    builder = ET.TreeBuilder()
    depth, nodes = 0, 0

    def tag(name):
        return "{" + name if "}" in name else name

    def start(name, attributes):
        nonlocal depth, nodes
        depth += 1
        nodes += 1
        if depth > MAX_XML_DEPTH or nodes > MAX_XML_NODES:
            raise DocumentTextError(f"XML {kind} превышает лимит глубины или числа элементов.")
        builder.start(tag(name), {tag(key): value for key, value in attributes.items()})

    def end(name):
        nonlocal depth
        builder.end(tag(name))
        depth -= 1

    def forbidden(*args):
        raise DocumentTextError(f"DTD и объявления XML-сущностей в {kind} запрещены.")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SkippedEntityHandler = forbidden
    try:
        parser.Parse(xml, True)
        return builder.close()
    except (expat.ExpatError, ET.ParseError):
        raise DocumentTextError(f"Основной XML {kind} повреждён.") from None


class _BodyText:
    def __init__(self, namespace):
        self.prefix = "{" + namespace + "}"
        self.paragraphs = 0
        self.tables = 0
        self.text_bytes = 0
        self.skipped = {self.tag(name) for name in ("del", "instrText", "drawing", "pict", "object")}

    def tag(self, name):
        return self.prefix + name

    def add(self, text):
        # Count separators too; the complete result always fits this budget.
        self.text_bytes += len(text.encode("utf-8"))
        if self.text_bytes > MAX_TEXT_BYTES:
            raise DocumentTextError("Извлечённый текст DOCX превышает лимит 256 КиБ.")
        return text

    def paragraph(self, node):
        parts = []

        def visit(element):
            if element.tag in self.skipped:
                return
            if element.tag == self.tag("t"):
                parts.append(self.add(element.text or ""))
            elif element.tag == self.tag("tab"):
                parts.append(self.add("\t"))
            elif element.tag in {self.tag("br"), self.tag("cr")}:
                parts.append(self.add("\n"))
            elif element.tag == self.tag("noBreakHyphen"):
                parts.append(self.add("\u2011"))
            elif element.tag == self.tag("softHyphen"):
                parts.append(self.add("\u00ad"))
            else:
                for child in element:
                    visit(child)

        visit(node)
        text = "".join(parts)
        if text.strip():
            self.paragraphs += 1
        return text

    def join(self, parts, separator):
        if len(parts) > 1:
            self.add(separator * (len(parts) - 1))
        return separator.join(parts)

    def children(self, node, name):
        for child in node:
            if child.tag == self.tag(name):
                yield child
            elif child.tag in {self.tag("sdt"), self.tag("sdtContent"), self.tag("customXml")}:
                yield from self.children(child, name)

    def table(self, node):
        self.tables += 1
        rows = []
        for row in self.children(node, "tr"):
            cells = []
            for cell in self.children(row, "tc"):
                # Cell paragraphs retain their line breaks; tabs separate cells.
                cells.append(self.blocks(cell, separator="\n"))
            rows.append(self.join(cells, "\t"))
        return self.join(rows, "\n")

    def blocks(self, node, separator="\n\n"):
        parts = []
        for child in node:
            if child.tag == self.tag("p"):
                text = self.paragraph(child)
            elif child.tag == self.tag("tbl"):
                text = self.table(child)
            elif child.tag in {self.tag("sdt"), self.tag("sdtContent"), self.tag("customXml")}:
                text = self.blocks(child, separator)
            else:
                continue
            if text.strip():
                parts.append(text)
        return self.join(parts, separator)


def extract_docx(raw, *, source):
    """Return text with its caller-validated path; never execute document data.

    Only the main Word body is read. Headers, footnotes, images/OCR, layout,
    relationships and embedded documents are deliberately outside this adapter.
    """
    root = _xml_tree(_main_xml(raw))
    namespace = next((ns for ns in WORD_NAMESPACES if root.tag == "{" + ns + "}document"), None)
    if namespace is None:
        raise DocumentTextError("В DOCX нет основной структуры WordprocessingML.")
    body = root.find("{" + namespace + "}body")
    if body is None:
        raise DocumentTextError("В DOCX нет основного тела документа.")
    renderer = _BodyText(namespace)
    text = renderer.blocks(body)
    return DocxText(str(source), text, renderer.paragraphs, renderer.tables)
