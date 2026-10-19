import sys
import tempfile
import zipfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import rag


class DocumentParsingTests(unittest.TestCase):
    def test_supported_office_formats_are_accepted(self):
        for suffix in (".docx", ".pptx", ".xlsx"):
            with self.subTest(suffix=suffix):
                self.assertEqual(rag.validate_upload(f"document{suffix}"), suffix)

    def test_legacy_formats_remain_supported(self):
        for suffix in (".pdf", ".md", ".markdown", ".txt", ".html", ".htm"):
            with self.subTest(suffix=suffix):
                self.assertEqual(rag.validate_upload(f"document{suffix}"), suffix)

    def test_office_document_uses_markitdown_without_plugins(self):
        converter = Mock()
        converter.convert.return_value = SimpleNamespace(markdown="# Extracted")
        markitdown_module = SimpleNamespace(
            MarkItDown=Mock(return_value=converter),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("document.xml", "test")
            with patch.dict(sys.modules, {"markitdown": markitdown_module}):
                result = rag._parse_file(path)

        self.assertEqual(result, "# Extracted")
        markitdown_module.MarkItDown.assert_called_once_with(enable_plugins=False)
        converter.convert.assert_called_once_with(str(path))

    def test_invalid_office_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.docx"
            path.write_text("not an office archive")
            with self.assertRaisesRegex(ValueError, "不是有效的文件"):
                rag._parse_file(path)

    def test_plain_text_keeps_utf8_replacement_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.txt"
            path.write_bytes(b"hello\xff")
            self.assertEqual(rag._parse_file(path), "hello\ufffd")

    def test_html_extracts_article_and_title_without_running_page_scripts(self):
        article = (
            "This saved article explains personal knowledge management and "
            "how source documents can be organized for reliable search. "
        ) * 8
        html = f"""<!doctype html>
        <html><head><title>Local Research Notes</title></head>
        <body><nav>Navigation noise</nav><main><article><h1>Research</h1>
        <p>{article}</p></article></main><script>UNTRUSTED_SCRIPT_TEXT</script></body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.html"
            path.write_text(html, encoding="utf-8")
            result = rag._parse_file(path)

        self.assertIn("Research", result)
        self.assertIn("personal knowledge management", result)
        self.assertNotIn("UNTRUSTED_SCRIPT_TEXT", result)

    def test_unsupported_format_is_rejected(self):
        with self.assertRaises(ValueError):
            rag.validate_upload("document.doc")


if __name__ == "__main__":
    unittest.main()
