"""Image-only receipt OCR and structured field extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterable


class ReceiptDocumentError(ValueError):
    """Raised when the uploaded image cannot produce a usable receipt result."""


PSM_MODES = (6, 4, 11, 3)
MAX_IMAGE_LONG_EDGE = 3000

_AMOUNT_TOKEN_RE = re.compile(
    r"(?<!\d)[0-9Oo](?:[0-9Oo,\s]*[0-9Oo])?(?:\s*원)?"
)
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})")
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:시]\s*(\d{2})")
_BUSINESS_NUMBER_RE = re.compile(r"(?<!\d)(\d{3})\D?(\d{2})\D?(\d{5})(?!\d)")


@dataclass(frozen=True)
class _OcrAttempt:
    variant: str
    psm: int
    text: str
    score: int


def _load_image_variants(image_bytes: bytes) -> list[tuple[str, Any]]:
    """Load a camera image and create a small set of OCR-friendly variants."""
    if not image_bytes:
        raise ReceiptDocumentError("영수증 이미지가 비어 있습니다.")

    try:
        from PIL import Image, ImageEnhance, ImageOps

        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")

        if max(image.size) > MAX_IMAGE_LONG_EDGE:
            ratio = MAX_IMAGE_LONG_EDGE / max(image.size)
            size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
            image = image.resize(size, Image.Resampling.LANCZOS)

        gray = ImageOps.grayscale(image)
        contrast = ImageEnhance.Contrast(gray).enhance(1.8)
        binary = contrast.point(lambda pixel: 255 if pixel >= 170 else 0)
        return [
            ("original", image),
            ("contrast", contrast),
            ("binary", binary),
        ]
    except ReceiptDocumentError:
        raise
    except Exception as error:
        raise ReceiptDocumentError("영수증 이미지 형식을 읽지 못했습니다.") from error


def _run_tesseract(image: Any, *, language: str, psm: int) -> str:
    try:
        import pytesseract

        return pytesseract.image_to_string(
            image,
            lang=language,
            config=f"--psm {psm}",
        ) or ""
    except Exception as error:
        raise ReceiptDocumentError("Tesseract OCR 실행에 실패했습니다.") from error


def _ocr_quality_score(text: str) -> int:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return 0

    score = min(len(normalized), 1200)
    score += 250 if re.search(r"합계|총액|결제금액|받을금액", normalized) else 0
    score += 150 if re.search(r"\d{3}\D?\d{2}\D?\d{5}", normalized) else 0
    score += 100 if _DATE_RE.search(normalized) else 0
    score += 100 if re.search(r"공급가액|부가세|VAT", normalized, re.IGNORECASE) else 0
    return score


def extract_receipt_text_from_image_bytes(
    image_bytes: bytes,
    language: str = "kor+eng",
) -> str:
    """OCR a camera image using several layouts and preprocessing variants."""
    attempts: list[_OcrAttempt] = []
    for variant_name, image in _load_image_variants(image_bytes):
        for psm in PSM_MODES:
            text = _run_tesseract(image, language=language, psm=psm)
            attempts.append(
                _OcrAttempt(
                    variant=variant_name,
                    psm=psm,
                    text=text,
                    score=_ocr_quality_score(text),
                )
            )

    best = max(attempts, key=lambda attempt: attempt.score, default=None)
    if best is None or not best.text.strip():
        raise ReceiptDocumentError("영수증에서 텍스트를 추출하지 못했습니다.")
    return best.text


def _clean_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]


def _parse_amount(raw: str) -> int | None:
    digits = re.sub(r"[^0-9Oo]", "", raw).replace("O", "0").replace("o", "0")
    if not digits:
        return None
    return int(digits)


def _amounts_on_line(line: str) -> list[int]:
    values: list[int] = []
    for token in _AMOUNT_TOKEN_RE.findall(line):
        value = _parse_amount(token)
        if value is not None:
            values.append(value)
    return values


def _labeled_amount(lines: Iterable[str], labels: tuple[str, ...]) -> int | None:
    for line in lines:
        if not any(label.lower() in line.lower() for label in labels):
            continue
        values = _amounts_on_line(line)
        if values:
            return values[-1]
    return None


def _extract_merchant(lines: list[str]) -> str | None:
    labels = ("상호명", "가맹점명", "상호")
    for line in lines:
        for label in labels:
            if label in line:
                value = line.split(label, 1)[1].lstrip(" :：-")
                if value:
                    return value

    ignored = ("사업자", "합계", "총액", "공급가액", "부가세", "결제", "신용카드", "체크카드")
    for line in lines:
        if (
            len(line) <= 60
            and not any(token in line for token in ignored)
            and not _DATE_RE.search(line)
            and not _amounts_on_line(line)
        ):
            return line
    return None


def _extract_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^(?P<name>.+?)\s+(?P<quantity>[0-9Oo]+)\s+(?P<amount>[0-9Oo][0-9Oo,\s]*)\s*원?$"
    )
    excluded = ("공급가액", "부가세", "합계", "총액", "결제", "사업자")
    for line in lines:
        if any(label in line for label in excluded) or _DATE_RE.search(line):
            continue
        match = pattern.match(line)
        if not match:
            continue
        amount = _parse_amount(match.group("amount"))
        quantity = _parse_amount(match.group("quantity"))
        name = match.group("name").strip(" :-")
        if amount is None or quantity is None or not name:
            continue
        items.append({
            "name": name,
            "quantity": quantity,
            "unitPrice": round(amount / quantity) if quantity else amount,
            "amount": amount,
        })
    return items


def parse_receipt_text(text: str) -> dict[str, Any]:
    """Convert OCR text into receipt fields and validation warnings."""
    lines = _clean_lines(text)
    if not lines:
        raise ReceiptDocumentError("영수증 OCR 결과가 비어 있습니다.")

    joined = "\n".join(lines)
    date_match = _DATE_RE.search(joined)
    time_match = _TIME_RE.search(joined)
    business_match = _BUSINESS_NUMBER_RE.search(joined)

    total = _labeled_amount(
        lines,
        ("총 결제금액", "총결제금액", "총액", "합계", "결제금액", "받을금액", "승인금액"),
    )
    if total is None:
        total = _labeled_amount(lines, ("금액",))
    if total is None or total <= 0:
        raise ReceiptDocumentError("영수증 총액을 확인하지 못했습니다.")

    supply = _labeled_amount(lines, ("공급가액", "공급가"))
    vat = _labeled_amount(lines, ("부가세", "VAT"))
    items = _extract_items(lines)
    warnings: list[str] = []

    if supply is not None and vat is not None and supply + vat != total:
        warnings.append("공급가액과 부가세 합계가 총액과 일치하지 않습니다.")
    if items and sum(item["amount"] for item in items) != total:
        warnings.append("품목 합계가 총액과 일치하지 않습니다.")

    payment_method = None
    for method in ("신용카드", "체크카드", "현금", "간편결제", "카카오페이", "네이버페이"):
        if method in joined:
            payment_method = method
            break

    merchant = _extract_merchant(lines)
    if merchant is None:
        warnings.append("상호명을 확인하지 못했습니다.")
    if date_match is None:
        warnings.append("거래일자를 확인하지 못했습니다.")
    if business_match is None:
        warnings.append("사업자등록번호를 확인하지 못했습니다.")

    confidence_fields = [merchant, date_match, business_match, supply, vat, total]
    confidence = round(sum(value is not None for value in confidence_fields) / len(confidence_fields), 2)

    return {
        "merchantName": merchant,
        "businessNumber": (
            f"{business_match.group(1)}-{business_match.group(2)}-{business_match.group(3)}"
            if business_match else None
        ),
        "transactionDate": (
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            if date_match else None
        ),
        "transactionTime": (
            f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
            if time_match else None
        ),
        "approvalNumber": None,
        "supplyAmount": supply,
        "vat": vat,
        "totalAmount": total,
        "paymentMethod": payment_method,
        "items": items,
        "confidence": confidence,
        "warnings": warnings,
    }


def analyze_receipt_image_bytes(
    image_bytes: bytes,
    language: str = "kor+eng",
) -> dict[str, Any]:
    text = extract_receipt_text_from_image_bytes(image_bytes, language=language)
    result = parse_receipt_text(text)
    return {"result": result, "rawOcrText": text}
