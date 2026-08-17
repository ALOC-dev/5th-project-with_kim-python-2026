"""주민등록초본 OCR 전용 SQS Lambda 핸들러."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from residence_address_extractor import (
    ResidenceDocumentError,
    extract_residence_addresses,
    extract_residence_text_from_pdf_bytes,
)


MESSAGE_TYPE = "RESIDENCE_ADDRESS_EXTRACTION"
RESULT_QUEUE_URL = os.environ.get("RESULT_QUEUE_URL", "").strip()
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "kor+eng").strip() or "kor+eng"
OCR_DPI_RAW = os.environ.get("OCR_DPI", "300").strip()

s3 = boto3.client("s3")
sqs = boto3.client("sqs")


def _ocr_dpi() -> int:
    try:
        dpi = int(OCR_DPI_RAW)
    except ValueError as error:
        raise RuntimeError("OCR_DPI는 정수여야 합니다.") from error
    if not 150 <= dpi <= 600:
        raise RuntimeError("OCR_DPI는 150에서 600 사이여야 합니다.")
    return dpi


def _validate_request(message: dict[str, Any]) -> tuple[int, str, str]:
    if message.get("messageType") != MESSAGE_TYPE:
        raise ValueError("지원하지 않는 초본 요청 messageType입니다.")

    user_id = message.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError("초본 주소 추출 요청에 정수 userId가 없습니다.")

    source = message.get("source")
    if not isinstance(source, dict):
        raise ValueError("초본 주소 추출 요청에 source가 없습니다.")
    bucket = source.get("bucket")
    key = source.get("key")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("초본 주소 추출 요청에 source.bucket이 없습니다.")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("초본 주소 추출 요청에 source.key가 없습니다.")
    return user_id, bucket.strip(), key.strip()


def _send_result(payload: dict[str, Any]) -> None:
    if not RESULT_QUEUE_URL:
        raise RuntimeError("RESULT_QUEUE_URL이 설정되지 않았습니다.")

    request: dict[str, Any] = {
        "QueueUrl": RESULT_QUEUE_URL,
        "MessageBody": json.dumps(payload, ensure_ascii=False),
    }
    if RESULT_QUEUE_URL.endswith(".fifo"):
        request["MessageGroupId"] = "residence-address-results"
        request["MessageDeduplicationId"] = (
            f"{MESSAGE_TYPE}:{payload['userId']}:{payload['status']}"
        )

    response = sqs.send_message(**request)
    print(
        "[info] 초본 결과 SQS 발행 성공: "
        f"userId={payload['userId']}, status={payload['status']}, "
        f"messageId={response.get('MessageId')}"
    )


def _process_message(message: dict[str, Any]) -> None:
    user_id, bucket, key = _validate_request(message)
    print(
        f"[info] 초본 OCR 시작: userId={user_id}, "
        f"s3://{bucket}/{key}"
    )

    response = s3.get_object(Bucket=bucket, Key=key)
    pdf_bytes = response["Body"].read()
    print(f"[info] 초본 PDF 다운로드 완료: userId={user_id}, bytes={len(pdf_bytes)}")

    try:
        text = extract_residence_text_from_pdf_bytes(
            pdf_bytes,
            language=OCR_LANGUAGE,
            dpi=_ocr_dpi(),
        )
        print(f"[info] 초본 OCR 완료: userId={user_id}, chars={len(text)}")
        addresses = extract_residence_addresses(text)
        payload = {
            "messageType": MESSAGE_TYPE,
            "userId": user_id,
            "status": "COMPLETED",
            "addresses": addresses,
        }
    except ResidenceDocumentError as error:
        payload = {
            "messageType": MESSAGE_TYPE,
            "userId": user_id,
            "status": "FAILED",
            "error": str(error),
            "addresses": [],
        }

    _send_result(payload)
    print(
        f"[info] 초본 OCR 처리 완료: userId={user_id}, "
        f"status={payload['status']}, addresses={len(payload['addresses'])}"
    )


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SQS 레코드를 처리하고 실패한 레코드만 재시도 대상으로 반환한다."""
    del context
    batch_item_failures: list[dict[str, str]] = []
    processed = 0

    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "?"))
        try:
            body = record.get("body")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("SQS 레코드에 body가 없습니다.")
            message = json.loads(body)
            if not isinstance(message, dict):
                raise ValueError("SQS body는 JSON 객체여야 합니다.")
            _process_message(message)
            processed += 1
        except Exception as error:
            print(
                f"[error] 초본 OCR 처리 실패: messageId={message_id}, "
                f"errorType={type(error).__name__}, error={error}"
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    print(
        f"[info] 초본 OCR 배치 완료: processed={processed}, "
        f"failed={len(batch_item_failures)}"
    )
    return {"batchItemFailures": batch_item_failures}
