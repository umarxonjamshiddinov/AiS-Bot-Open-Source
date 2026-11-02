from __future__ import annotations

import csv
import io
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader


TEXT_EXTENSIONS = {
    ".txt", ".md", ".log", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json", ".xml", ".yaml",
    ".yml", ".sql", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".sh",
    ".bash", ".bat", ".cmd", ".ps1", ".ini", ".cfg", ".conf",
    ".toml", ".properties", ".env",
}
TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
DOCUMENT_EXTENSIONS = {".docx", ".pdf"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | TABLE_EXTENSIONS | DOCUMENT_EXTENSIONS
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TEXT_CHARS = 300_000


class ImageOnlyPdfError(ValueError):
    """The PDF has no extractable text and needs a future page-image adapter."""


def read_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _read_delimited(file_bytes: bytes, delimiter: str | None = None) -> str:
    text = read_text(file_bytes)
    if delimiter is None:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
    else:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return "\n".join(" | ".join(str(value) for value in row) for row in reader)


def read_xlsx(file_bytes: bytes) -> str:
    workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    result: list[str] = []
    try:
        for sheet in workbook.worksheets:
            result.append(f"\n=== Sheet: {sheet.title} ===")
            for row in sheet.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                if any(values):
                    result.append(" | ".join(values))
    finally:
        workbook.close()
    return "\n".join(result)


def read_docx(file_bytes: bytes) -> str:
    document = Document(io.BytesIO(file_bytes))
    result = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for number, table in enumerate(document.tables, start=1):
        result.append(f"\n=== Table {number} ===")
        result.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows)
    return "\n".join(result)


def read_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    result: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        if text and text.strip():
            result.append(f"\n=== Page {page_number} ===\n{text.strip()}")
    if not result:
        raise ImageOnlyPdfError(
            "This PDF has no text layer. Scanned-page image processing "
            "is not available yet."
        )
    return "\n".join(result)


def limit_text(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "\n\n[Document too large. Only the first part is shown.]"


def extract_document(file_bytes: bytes, filename: str) -> str:
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError("File too large. Maximum size is 20 MB.")
    extension = Path(filename).suffix.lower()
    if not extension:
        raise ValueError("The file has no extension.")
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"The {extension} format is not supported yet.")
    if extension in TEXT_EXTENSIONS:
        result = read_text(file_bytes)
    elif extension == ".csv":
        result = _read_delimited(file_bytes)
    elif extension == ".tsv":
        result = _read_delimited(file_bytes, "\t")
    elif extension == ".xlsx":
        result = read_xlsx(file_bytes)
    elif extension == ".docx":
        result = read_docx(file_bytes)
    elif extension == ".pdf":
        result = read_pdf(file_bytes)
    else:
        raise ValueError(f"No reader is available for {extension}.")
    result = result.strip()
    if not result:
        raise ValueError("No text could be extracted from the document.")
    return limit_text(result)


def is_supported_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
