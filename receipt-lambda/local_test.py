"""Run receipt OCR against one local image without AWS services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from receipt_ocr import ReceiptDocumentError, analyze_receipt_image_bytes


def analyze_file(image_path: str | Path, language: str = "kor+eng") -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {path}")

    analysis = analyze_receipt_image_bytes(path.read_bytes(), language=language)
    return {
        "status": "COMPLETED",
        "result": analysis["result"],
        # OCR 원문은 영수증 개인정보가 포함될 수 있어 콘솔에 출력하지 않는다.
        "rawOcrCharCount": len(analysis.get("rawOcrText", "")),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local receipt image OCR")
    parser.add_argument("image", help="receipt image path")
    parser.add_argument("--language", default="kor+eng")
    args = parser.parse_args(argv)

    try:
        output = analyze_file(args.image, language=args.language)
    except ReceiptDocumentError as error:
        output = {"status": "FAILED", "error": str(error)}
    except (OSError, ValueError) as error:
        output = {"status": "FAILED", "error": str(error)}

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
