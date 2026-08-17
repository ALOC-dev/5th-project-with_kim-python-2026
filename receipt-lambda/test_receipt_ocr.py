import unittest
from unittest.mock import patch

from receipt_ocr import (
    ReceiptDocumentError,
    analyze_receipt_image_bytes,
    parse_receipt_text,
)


OCR_TEXT = """
카페 파도
사업자등록번호 123-45-67890
2026-08-01 14:32
아메리카노 2 8,000원
공급가액 7,273원
부가세 727원
합계 8,000원
신용카드
"""


class ReceiptParserTest(unittest.TestCase):
    def test_extracts_receipt_fields_and_validates_amounts(self):
        result = parse_receipt_text(OCR_TEXT)

        self.assertEqual(result["merchantName"], "카페 파도")
        self.assertEqual(result["businessNumber"], "123-45-67890")
        self.assertEqual(result["transactionDate"], "2026-08-01")
        self.assertEqual(result["transactionTime"], "14:32")
        self.assertEqual(result["supplyAmount"], 7273)
        self.assertEqual(result["vat"], 727)
        self.assertEqual(result["totalAmount"], 8000)
        self.assertEqual(result["paymentMethod"], "신용카드")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["items"][0]["name"], "아메리카노")
        self.assertEqual(result["items"][0]["quantity"], 2)
        self.assertEqual(result["items"][0]["amount"], 8000)

    def test_keeps_warning_when_amounts_do_not_match(self):
        result = parse_receipt_text("""
        카페 파도
        합계 10,000원
        공급가액 8,000원
        부가세 500원
        """)

        self.assertIn("공급가액과 부가세 합계", " ".join(result["warnings"]))

    def test_requires_a_total_amount(self):
        with self.assertRaises(ReceiptDocumentError):
            parse_receipt_text("카페 파도\n2026-08-01\n아메리카노")


class ReceiptOcrTest(unittest.TestCase):
    @patch("receipt_ocr._run_tesseract")
    @patch("receipt_ocr._load_image_variants")
    def test_uses_korean_english_ocr_and_selects_best_variant(
        self,
        load_variants,
        run_tesseract,
    ):
        load_variants.return_value = [("original", object()), ("contrast", object())]
        run_tesseract.side_effect = [
            "짧은 결과",
            OCR_TEXT,
            "",
            "오래된 결과",
            "",
            "",
            "",
            "",
        ]

        result = analyze_receipt_image_bytes(b"image-bytes", language="kor+eng")

        self.assertEqual(result["result"]["totalAmount"], 8000)
        self.assertEqual(run_tesseract.call_args_list[0].kwargs["language"], "kor+eng")
        self.assertEqual(run_tesseract.call_args_list[0].kwargs["psm"], 6)
        self.assertEqual(run_tesseract.call_count, 8)


if __name__ == "__main__":
    unittest.main()
