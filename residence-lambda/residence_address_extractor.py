"""주민등록초본 이미지를 OCR하고 주소별 변동 연도를 집계한다."""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from typing import Any


class ResidenceDocumentError(ValueError):
    """PDF 내용이 없거나 초본 주소 기록을 판독할 수 없을 때 발생한다."""


_REGION_START_RE = re.compile(
    r"(서울(?:특별시|시)?|부산(?:광역시|시)?|대구(?:광역시|시)?|"
    r"인천(?:광역시|시)?|광주(?:광역시|시)?|대전(?:광역시|시)?|"
    r"울산(?:광역시|시)?|세종(?:특별자치시|시)?|경기도|"
    r"강원(?:특별자치도|도)|충청북도|충청남도|전북특별자치도|"
    r"전라북도|전라남도|경상북도|경상남도|제주특별자치도)"
)
_LOCAL_ADDRESS_START_RE = re.compile(
    r"(?<![가-힣])(?:[가-힣]{2,12}(?:시|군|구))\s+"
    r"[가-힣0-9]{1,20}(?:동|읍|면|리|가|대로|로|길)(?=\s|\d)"
)
_DATE_RE = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*"
    r"(?:년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?|"
    r"[./-]\s*\d{1,2}(?:\s*[./-]\s*\d{1,2})?)"
)
_EVENT_RE = re.compile(
    r"(?:전\s*[입임]|세대주\s*변경|통반\s*변경|상세\s*주소\s*변경|"
    r"주소\s*변경|동번\s*변경|도로명\s*주소(?:\s*변경)?|지번\s*변경)"
)
_ROAD_ADDRESS_RE = re.compile(r"(?:대로|로|길)\s*\d")
_JIBUN_ADDRESS_RE = re.compile(r"(?:동|읍|면|리|가)\s*(?:산\s*)?\d")
_ADDRESS_CONTINUATION_RE = re.compile(
    r"(?:아파트|빌라|연립|주택|오피스텔|동|호|층|산|\d)"
)
_ADDRESS_STOP_RE = re.compile(
    r"\s+(?:발생일|신고일|사유|세대주|관계|전입|주소변경|"
    r"상세주소변경|동번변경|도로명주소).*$"
)


def extract_residence_text_from_pdf_bytes(
    pdf_bytes: bytes,
    language: str = "kor+eng",
    dpi: int = 300,
) -> str:
    """PDF의 모든 페이지를 이미지로 렌더링하고 Tesseract OCR을 수행한다."""
    if not pdf_bytes:
        raise ResidenceDocumentError("초본 PDF가 비어 있습니다.")
    if not 150 <= dpi <= 600:
        raise ValueError("OCR DPI는 150에서 600 사이여야 합니다.")
    if not language or not language.strip():
        raise ValueError("OCR 언어가 비어 있습니다.")

    try:
        import fitz
    except ImportError as error:
        raise RuntimeError("PDF 렌더링에 필요한 PyMuPDF가 없습니다.") from error

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as error:
        raise ResidenceDocumentError("올바른 초본 PDF를 열 수 없습니다.") from error

    page_texts: list[str] = []
    try:
        if len(document) == 0:
            raise ResidenceDocumentError("초본 PDF에 페이지가 없습니다.")

        try:
            from PIL import Image
            import pytesseract
        except ImportError as error:
            raise RuntimeError("PDF OCR 실행에 필요한 라이브러리가 없습니다.") from error

        scale = dpi / 72
        matrix = fitz.Matrix(scale, scale)
        for page in document:
            try:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image = Image.frombytes(
                    "RGB",
                    (pixmap.width, pixmap.height),
                    pixmap.samples,
                )
                try:
                    page_texts.append(
                        pytesseract.image_to_string(
                            image,
                            lang=language.strip(),
                            config="--psm 6",
                        )
                    )
                finally:
                    close_image = getattr(image, "close", None)
                    if callable(close_image):
                        close_image()
            except ResidenceDocumentError:
                raise
            except Exception as error:
                raise RuntimeError("초본 PDF 이미지 OCR 처리에 실패했습니다.") from error
    finally:
        document.close()

    text = "\n".join(page_texts).strip()
    if not text:
        raise ResidenceDocumentError("초본 PDF에서 OCR 텍스트를 추출하지 못했습니다.")
    return text


def extract_residence_addresses(text: str) -> list[dict[str, Any]]:
    """OCR 텍스트에서 주소와 명시된 전입·변동 연도만 집계한다."""
    if not text or not text.strip():
        raise ResidenceDocumentError("초본 OCR 텍스트가 비어 있습니다.")

    lines = [_normalize_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    address_indexes = [
        index for index, line in enumerate(lines) if _address_start(line)
    ]

    records: OrderedDict[str, dict[str, Any]] = OrderedDict()
    latest_key: str | None = None

    for position, line_index in enumerate(address_indexes):
        next_address_index = (
            address_indexes[position + 1]
            if position + 1 < len(address_indexes)
            else len(lines)
        )
        forward_end = min(next_address_index, line_index + 5)
        forward_lines = lines[line_index:forward_end]

        event_year = _find_event_year(forward_lines)
        if event_year is None and position == 0:
            event_year = _find_event_year(lines[max(0, line_index - 3):line_index])
        if event_year is None:
            continue

        address = _extract_address(forward_lines)
        if not address:
            continue

        key = _address_key(address)
        if key not in records:
            records[key] = {
                "rawAddress": address,
                "roadAddress": address if _ROAD_ADDRESS_RE.search(address) else None,
                "jibunAddress": address if _JIBUN_ADDRESS_RE.search(address) else None,
                "current": False,
                "residenceYears": [],
            }

        years = records[key]["residenceYears"]
        if event_year not in years:
            years.append(event_year)
        latest_key = key

    if not records:
        raise ResidenceDocumentError(
            "주민등록초본에서 주소와 변동 연도를 찾지 못했습니다."
        )

    if latest_key is not None:
        records[latest_key]["current"] = True
    return list(records.values())


def _normalize_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line)
    normalized = normalized.replace("\u200b", " ").replace("—", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def _find_event_year(lines: list[str]) -> str | None:
    block = " ".join(lines)
    event = _EVENT_RE.search(block)
    if not event:
        return None

    dates = list(_DATE_RE.finditer(block))
    if not dates:
        return None

    nearest = min(dates, key=lambda match: abs(match.start() - event.start()))
    return nearest.group("year")


def _extract_address(lines: list[str]) -> str | None:
    if not lines:
        return None

    address_start = _address_start(lines[0])
    if not address_start:
        return None

    first = lines[0][address_start.start():]
    first = _cut_before_metadata(first)
    parts = [first] if first else []

    for continuation in lines[1:3]:
        if _address_start(continuation):
            break
        candidate = _cut_before_metadata(continuation)
        if not candidate or _DATE_RE.search(continuation) or _EVENT_RE.search(continuation):
            break
        if not _ADDRESS_CONTINUATION_RE.search(candidate):
            break
        parts.append(candidate)

    address = re.sub(r"\s+", " ", " ".join(parts)).strip(" ,")
    address = _ADDRESS_STOP_RE.sub("", address).strip(" ,")
    return address or None


def _cut_before_metadata(value: str) -> str:
    cut_positions = [
        match.start()
        for pattern in (_DATE_RE, _EVENT_RE)
        if (match := pattern.search(value)) is not None
    ]
    if cut_positions:
        value = value[:min(cut_positions)]
    return value.strip(" ,|-")


def _address_key(address: str) -> str:
    normalized = unicodedata.normalize("NFKC", address)
    replacements = {
        "서울특별시": "서울",
        "서울시": "서울",
        "부산광역시": "부산",
        "대구광역시": "대구",
        "인천광역시": "인천",
        "광주광역시": "광주",
        "대전광역시": "대전",
        "울산광역시": "울산",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    local_start = _LOCAL_ADDRESS_START_RE.search(normalized)
    if local_start:
        normalized = normalized[local_start.start():]
    return re.sub(r"[^가-힣0-9]", "", normalized)


def _address_start(line: str):
    candidates = [
        match
        for pattern in (_REGION_START_RE, _LOCAL_ADDRESS_START_RE)
        if (match := pattern.search(line)) is not None
    ]
    return min(candidates, key=lambda match: match.start()) if candidates else None
