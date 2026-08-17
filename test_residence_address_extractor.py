import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from residence_address_extractor import (
    extract_residence_addresses,
    extract_residence_text_from_pdf_bytes,
)


class ResidenceAddressExtractorTest(unittest.TestCase):
    def test_pdf_extraction_skips_expensive_table_analysis_and_releases_pages(self):
        page = MagicMock()
        page.extract_text.return_value = "2007년 서울특별시 동대문구 회기동 62-8"
        pdf = MagicMock()
        pdf.pages = [page]
        pdf_context = MagicMock()
        pdf_context.__enter__.return_value = pdf
        fake_pdfplumber = SimpleNamespace(open=MagicMock(return_value=pdf_context))

        with patch.dict("sys.modules", {"pdfplumber": fake_pdfplumber}):
            text = extract_residence_text_from_pdf_bytes(b"pdf")

        self.assertIn("회기동 62-8", text)
        page.extract_text.assert_called_once()
        page.extract_tables.assert_not_called()
        page.close.assert_called_once()

    def test_groups_non_contiguous_years_for_the_same_address(self):
        text = """
        주소변동사항
        2007년 3월 2일 서울특별시 동대문구 회기동 62-8
        2008년 4월 1일 서울특별시 동대문구 전농동 152-73
        2009년 5월 1일 서울시 동대문구 회기동 62-8
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0]["rawAddress"], "서울특별시 동대문구 회기동 62-8")
        self.assertEqual(addresses[0]["residenceYears"], ["2007", "2009"])
        self.assertTrue(addresses[0]["current"])
        self.assertEqual(addresses[1]["residenceYears"], ["2008"])
        self.assertFalse(addresses[1]["current"])

    def test_preserves_explicit_year_range(self):
        text = """
        주소변동사항
        2007년 ~ 2010년
        서울특별시 동대문구 전농동 152-73
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]["residenceYears"], ["2007~2010"])
        self.assertTrue(addresses[0]["current"])

    def test_reduces_full_date_range_to_year_range(self):
        text = """
        2007년 3월 2일 ~ 2010년 8월 14일
        서울특별시 동대문구 전농동 152-73
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(addresses[0]["residenceYears"], ["2007~2010"])

    def test_joins_date_line_with_following_address_line(self):
        text = """
        전입일자 2021년 7월 19일
        서울특별시 동대문구 회기로18길 46 제502호
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(addresses[0]["residenceYears"], ["2021"])
        self.assertEqual(
            addresses[0]["roadAddress"],
            "서울특별시 동대문구 회기로18길 46 제502호",
        )


if __name__ == "__main__":
    unittest.main()
