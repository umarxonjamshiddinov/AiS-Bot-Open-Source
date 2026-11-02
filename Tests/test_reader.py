import io
import unittest

from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter

from Reader.documents import ImageOnlyPdfError, extract_document


def workbook_bytes() -> bytes:
    workbook = Workbook()
    workbook.active.title = "Working Models"
    workbook.active.append(["model_id"])
    workbook.active.append(["openai.gpt-oss-120b"])
    workbook.create_sheet("Notes").append(["Generated test fixture"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class ReaderTests(unittest.TestCase):
    def test_text_csv_and_tsv(self) -> None:
        self.assertEqual(extract_document("Привет".encode(), "note.txt"), "Привет")
        self.assertIn("a | b", extract_document(b"a,b\n1,2", "data.csv"))
        self.assertIn("a | b", extract_document(b"a\tb\n1\t2", "data.tsv"))

    def test_xlsx_catalog_is_readable(self) -> None:
        text = extract_document(workbook_bytes(), "catalog.xlsx")
        self.assertIn("openai.gpt-oss-120b", text)
        self.assertIn("=== Sheet: Notes ===", text)

    def test_docx_is_readable(self) -> None:
        document = Document()
        document.add_paragraph("AiS document test")
        buffer = io.BytesIO()
        document.save(buffer)
        self.assertIn(
            "AiS document test", extract_document(buffer.getvalue(), "document.docx")
        )

    def test_image_only_pdf_has_explicit_future_adapter_error(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaises(ImageOnlyPdfError):
            extract_document(buffer.getvalue(), "scan.pdf")
