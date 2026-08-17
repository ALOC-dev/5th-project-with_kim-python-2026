import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_test import analyze_file


class LocalReceiptRunnerTest(unittest.TestCase):
    @patch("local_test.analyze_receipt_image_bytes")
    def test_reads_image_path_and_returns_safe_summary(self, analyze):
        analyze.return_value = {
            "result": {
                "merchantName": "카페 파도",
                "totalAmount": 8000,
                "warnings": [],
            },
            "rawOcrText": "개인정보가 포함된 OCR 원문",
        }

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "receipt.jpg"
            image_path.write_bytes(b"image-bytes")

            result = analyze_file(image_path)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result"]["totalAmount"], 8000)
        self.assertEqual(result["rawOcrCharCount"], len("개인정보가 포함된 OCR 원문"))
        self.assertNotIn("rawOcrText", result)
        analyze.assert_called_once_with(b"image-bytes", language="kor+eng")


if __name__ == "__main__":
    unittest.main()
