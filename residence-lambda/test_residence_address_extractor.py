import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from residence_address_extractor import (
    ResidenceDocumentError,
    extract_residence_addresses,
    extract_residence_text_from_pdf_bytes,
)


class FakePage:
    def __init__(self):
        self.get_text = MagicMock()

    def get_pixmap(self, *, matrix, alpha):
        return SimpleNamespace(width=2, height=2, samples=b"\xff" * 12)


class FakeDocument:
    def __init__(self, pages):
        self._pages = pages
        self.closed = False

    def __len__(self):
        return len(self._pages)

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


class ResidencePdfOcrTest(unittest.TestCase):
    def test_ocr_renders_every_page_without_reading_text_layer(self):
        pages = [FakePage(), FakePage()]
        document = FakeDocument(pages)
        fitz = SimpleNamespace(
            open=MagicMock(return_value=document),
            Matrix=lambda x, y: (x, y),
        )
        image = MagicMock()
        image_module = SimpleNamespace(frombytes=MagicMock(return_value=image))
        ocr = MagicMock(side_effect=["2007-04-11 전입", "경기도 하남시 신장동 467-5"])
        pytesseract = SimpleNamespace(image_to_string=ocr)

        with patch.dict(
            sys.modules,
            {
                "fitz": fitz,
                "PIL": SimpleNamespace(Image=image_module),
                "pytesseract": pytesseract,
            },
        ):
            text = extract_residence_text_from_pdf_bytes(b"pdf")

        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(ocr.call_args_list[0].kwargs["lang"], "kor+eng")
        self.assertEqual(ocr.call_args_list[0].kwargs["config"], "--psm 6")
        self.assertIn("전입", text)
        for page in pages:
            page.get_text.assert_not_called()
        self.assertTrue(document.closed)

    def test_rejects_empty_and_page_less_pdf(self):
        with self.assertRaisesRegex(ResidenceDocumentError, "비어"):
            extract_residence_text_from_pdf_bytes(b"")

        document = FakeDocument([])
        fitz = SimpleNamespace(
            open=MagicMock(return_value=document),
            Matrix=lambda x, y: (x, y),
        )
        with patch.dict(sys.modules, {"fitz": fitz}):
            with self.assertRaisesRegex(ResidenceDocumentError, "페이지"):
                extract_residence_text_from_pdf_bytes(b"pdf")
        self.assertTrue(document.closed)

    def test_rejects_blank_ocr_result(self):
        document = FakeDocument([FakePage()])
        fitz = SimpleNamespace(
            open=MagicMock(return_value=document),
            Matrix=lambda x, y: (x, y),
        )
        image_module = SimpleNamespace(frombytes=MagicMock(return_value=MagicMock()))
        pytesseract = SimpleNamespace(image_to_string=MagicMock(return_value="  \n"))

        with patch.dict(
            sys.modules,
            {
                "fitz": fitz,
                "PIL": SimpleNamespace(Image=image_module),
                "pytesseract": pytesseract,
            },
        ):
            with self.assertRaisesRegex(ResidenceDocumentError, "OCR"):
                extract_residence_text_from_pdf_bytes(b"pdf")


class ResidenceAddressParserTest(unittest.TestCase):
    def test_merges_only_explicit_change_years_without_inferred_ranges(self):
        text = """
        1 경기도 하남시 덕풍동 365-18 2001-11-12 출생등록
        2 경기도 하남시 덕풍동 365-18 2002-05-31 세대주변경
        3 경기도 하남시 덕풍동 346 서해아파트 101-612 2002-10-04 전입
        4 경기도 하남시 덕풍동 365-18 2009-12-07 전입
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0]["residenceYears"], ["2002", "2009"])
        self.assertNotIn("2002~2009", addresses[0]["residenceYears"])
        self.assertTrue(addresses[0]["current"])
        self.assertFalse(addresses[1]["current"])

    def test_joins_ocr_split_address_date_and_event_lines(self):
        text = """
        경기도 하남시 덕풍동 690
        케이씨씨아파트 103-2001
        2006-06-15
        전입
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 1)
        self.assertEqual(
            addresses[0]["rawAddress"],
            "경기도 하남시 덕풍동 690 케이씨씨아파트 103-2001",
        )
        self.assertEqual(addresses[0]["residenceYears"], ["2006"])
        self.assertEqual(addresses[0]["jibunAddress"], addresses[0]["rawAddress"])

    def test_classifies_road_address_and_marks_last_valid_address_current(self):
        text = """
        2016년 1월 14일 전입
        경기도 하남시 서하남로605번길 20-25 (교산동)
        경기도 하남시 신장1로27번길 18-5 2017.02.17 전입
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0]["residenceYears"], ["2016"])
        self.assertFalse(addresses[0]["current"])
        self.assertEqual(addresses[1]["residenceYears"], ["2017"])
        self.assertTrue(addresses[1]["current"])
        self.assertEqual(addresses[1]["roadAddress"], addresses[1]["rawAddress"])

    def test_rejects_text_without_address_change_records(self):
        text = """
        문서 발급일 2026년 7월 31일
        경기도 하남시장
        경기도 하남시 덕풍동 365-18 2001-11-12 출생등록
        """

        with self.assertRaisesRegex(ResidenceDocumentError, "주소와 변동 연도"):
            extract_residence_addresses(text)

    def test_accepts_city_address_when_ocr_drops_province_and_merges_it(self):
        text = """
        경기도 하남시 덕풍동 365-18 2002-01-01 전입
        2 871 하남시 덕풍동 365-18 2009-05-31 세대주변경
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]["residenceYears"], ["2002", "2009"])

    def test_accepts_common_ocr_error_and_district_change_label(self):
        text = """
        경기도 하남시 덕풍동 346 2002-10-04
        서해아파트 101-612 전임
        경기도 하남시 덕풍동 690 2006-09-13 통반변경
        """

        addresses = extract_residence_addresses(text)

        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0]["residenceYears"], ["2002"])
        self.assertEqual(addresses[1]["residenceYears"], ["2006"])


if __name__ == "__main__":
    unittest.main()
