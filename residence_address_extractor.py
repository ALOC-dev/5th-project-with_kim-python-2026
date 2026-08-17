"""주민등록초본 텍스트에서 주소와 거주 연도만 추출한다."""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from typing import Any


_REGION_START_RE = re.compile(
    r"(서울(?:특별시|시)?|부산광역시|대구광역시|인천광역시|광주광역시|"
    r"대전광역시|울산광역시|세종특별자치시|경기도|강원(?:특별자치도|도)|"
    r"충청북도|충청남도|전북특별자치도|전라북도|전라남도|"
    r"경상북도|경상남도|제주특별자치도)"
)
_YEAR_RANGE_RE = re.compile(
    r"((?:19|20)\d{2})"
    r"(?:\s*년(?:\s*\d{1,2}\s*월\s*\d{1,2}\s*일)?"
    r"|[./-]\s*\d{1,2}[./-]\s*\d{1,2})?"
    r"\s*[~～]\s*"
    r"((?:19|20)\d{2})"
    r"(?:\s*년(?:\s*\d{1,2}\s*월\s*\d{1,2}\s*일)?"
    r"|[./-]\s*\d{1,2}[./-]\s*\d{1,2})?"
)
_YEAR_RE = re.compile(r"((?:19|20)\d{2})(?=\s*년|[./-]\s*\d{1,2})")
_TRAILING_LABEL_RE = re.compile(
    r"\s+(?:전입|전출|신고|변동|주소이동|세대주|정정|재등록).*$"
)
_ROAD_ADDRESS_RE = re.compile(r"(?:대로|로|길)\s*\d")
_JIBUN_ADDRESS_RE = re.compile(r"(?:동|읍|면|리|가)\s*\d")


def extract_residence_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """초본 PDF에서 표 분석 없이 본문 텍스트만 추출한다."""
    import io
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            try:
                parts.append(page.extract_text() or "")
            finally:
                # pdfminer의 페이지 객체 캐시가 다음 페이지까지 누적되지 않게 한다.
                page.close()
    return "\n".join(parts)


def extract_residence_addresses(text: str) -> list[dict[str, Any]]:
    """
    텍스트형 주민등록초본에서 주소별 거주 연도를 집계한다.

    동일 주소가 여러 번 나오면 처음 등장한 주소 표기를 유지하고 연도만 합친다.
    명시된 연속 기간은 ``2007~2010``으로 보존하며, 개별 이력은
    ``["2007", "2009"]``처럼 중복 없이 저장한다.
    """
    if not text or not text.strip():
        raise ValueError("PDF에서 텍스트를 추출하지 못했습니다. 스캔 PDF는 지원하지 않습니다.")

    records: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pending_years: list[str] = []
    latest_key: str | None = None

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        years = _extract_years(line)
        address = _extract_address(line)

        if years and not address:
            pending_years = years
            continue
        if address and not years:
            years = pending_years
        if not address or not years:
            continue

        pending_years = []
        key = _address_key(address)
        if key not in records:
            records[key] = {
                "rawAddress": address,
                "roadAddress": address if _ROAD_ADDRESS_RE.search(address) else None,
                "jibunAddress": address if _JIBUN_ADDRESS_RE.search(address) else None,
                "current": False,
                "residenceYears": [],
            }

        for year in years:
            if year not in records[key]["residenceYears"]:
                records[key]["residenceYears"].append(year)
        latest_key = key

    if not records:
        raise ValueError("주민등록초본에서 주소와 거주 연도를 찾지 못했습니다.")

    if latest_key is not None:
        records[latest_key]["current"] = True
    return list(records.values())


def _normalize_line(line: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", line).replace("\u200b", " "),
    ).strip()


def _extract_years(line: str) -> list[str]:
    range_match = _YEAR_RANGE_RE.search(line)
    if range_match:
        return [f"{range_match.group(1)}~{range_match.group(2)}"]

    years: list[str] = []
    for match in _YEAR_RE.finditer(line):
        year = match.group(1)
        if year not in years:
            years.append(year)
    return years


def _extract_address(line: str) -> str | None:
    match = _REGION_START_RE.search(line)
    if not match:
        return None

    address = line[match.start():]
    address = _TRAILING_LABEL_RE.sub("", address)
    address = re.sub(r"\s+", " ", address).strip(" ,")
    return address or None


def _address_key(address: str) -> str:
    normalized = unicodedata.normalize("NFKC", address)
    normalized = normalized.replace("서울특별시", "서울").replace("서울시", "서울")
    return re.sub(r"[^가-힣0-9]", "", normalized)
