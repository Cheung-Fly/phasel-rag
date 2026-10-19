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
        for suffix in (".pdf", ".md", ".markdown", ".txt"):
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

    def test_unsupported_format_is_rejected(self):
        with self.assertRaises(ValueError):
            rag.validate_upload("document.doc")


if __name__ == "__main__":
    unittest.main()
