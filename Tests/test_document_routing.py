import io
import unittest

from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from Reader.documents import ImageOnlyPdfError, extract_document
from Tests.test_routing import generation, make_router


def docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("AiS DOCX local reader")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    workbook = Workbook()
    workbook.active.append(["model", "value"])
    workbook.active.append(["fixture", "local reader"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def text_pdf_bytes() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 150 Td (AiS PDF local reader) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class DocumentRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_local_reader_routes_only_to_selected(
        self, file_bytes: bytes, filename: str
    ) -> None:
        extracted = extract_document(file_bytes, filename)
        router, state, client, _ = make_router()
        key = (700, hash(filename) % 10_000)
        result = await router.ask_text(
            conversation_key=key,
            conversation_id=generation(state, key),
            model_key="gpt_oss_120b",
            prompt=extracted,
        )
        self.assertEqual(result.route, "text")
        self.assertEqual(result.inference_count, 1)
        self.assertFalse(result.helper_used)
        self.assertEqual([call["model"].key for call in client.calls], ["gpt_oss_120b"])

    async def test_txt_uses_local_reader_without_qwen(self) -> None:
        await self.assert_local_reader_routes_only_to_selected(b"plain text", "note.txt")

    async def test_docx_uses_local_reader_without_qwen(self) -> None:
        await self.assert_local_reader_routes_only_to_selected(docx_bytes(), "note.docx")

    async def test_xlsx_uses_local_reader_without_qwen(self) -> None:
        await self.assert_local_reader_routes_only_to_selected(
            xlsx_bytes(), "models.xlsx"
        )

    async def test_csv_and_tsv_use_local_reader_without_qwen(self) -> None:
        await self.assert_local_reader_routes_only_to_selected(b"a,b\n1,2", "data.csv")
        await self.assert_local_reader_routes_only_to_selected(b"a\tb\n1\t2", "data.tsv")

    async def test_text_pdf_uses_pypdf_without_qwen(self) -> None:
        data = text_pdf_bytes()
        self.assertIn("AiS PDF local reader", extract_document(data, "text.pdf"))
        await self.assert_local_reader_routes_only_to_selected(data, "text.pdf")

    async def test_scanned_pdf_extension_routes_are_prepared_without_inference(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaises(ImageOnlyPdfError):
            extract_document(buffer.getvalue(), "scan.pdf")

        router, _, client, _ = make_router()
        self.assertEqual(router.visual_route("grok_4_3"), "ais_vision")
        self.assertEqual(router.visual_route("gpt_oss_120b"), "ais_vision")
        self.assertEqual(client.calls, [])
