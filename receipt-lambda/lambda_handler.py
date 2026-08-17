"""S3/SQS triggered image receipt OCR Lambda."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote_plus

from receipt_ocr import ReceiptDocumentError, analyze_receipt_image_bytes


RESULT_QUEUE_URL = os.environ.get("RESULT_QUEUE_URL", "").strip()
RAW_RESULT_BUCKET = os.environ.get("RAW_RESULT_BUCKET", "").strip()
RAW_RESULT_PREFIX = os.environ.get("RAW_RESULT_PREFIX", "ocr-results/RECEIPT").strip("/")
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "kor+eng").strip() or "kor+eng"

# Clients are lazy so parser tests can run without boto3 installed.
s3 = None
sqs = None


def _s3_client():
    global s3
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    return s3


def _sqs_client():
    global sqs
    if sqs is None:
        import boto3

        sqs = boto3.client("sqs")
    return sqs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_request(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("documentType") != "RECEIPT":
        raise ValueError("지원하지 않는 documentType입니다.")

    source = message.get("source") or {}
    bucket = message.get("s3Bucket") or source.get("bucket")
    key = message.get("s3Key") or source.get("key")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("영수증 요청에 S3 bucket이 없습니다.")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("영수증 요청에 S3 key가 없습니다.")

    document_id = message.get("documentId") or _document_id_from_key(key)
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("영수증 요청에 documentId가 없습니다.")

    return {
        **message,
        "s3Bucket": bucket.strip(),
        "s3Key": key.strip(),
        "documentId": document_id.strip(),
        "requestId": str(message.get("requestId") or f"receipt:{document_id}"),
    }


def _document_id_from_key(key: str) -> str:
    parts = key.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "receipts":
        return parts[2]
    filename = parts[-1] if parts else "receipt"
    return filename.rsplit(".", 1)[0] or "receipt"


def _user_id_from_key(key: str) -> int | None:
    parts = key.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "receipts":
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def _request_from_s3_record(record: dict[str, Any]) -> dict[str, Any]:
    s3_data = record.get("s3") or {}
    bucket_data = s3_data.get("bucket") or {}
    object_data = s3_data.get("object") or {}
    bucket = bucket_data.get("name")
    key = object_data.get("key")
    if not isinstance(bucket, str) or not isinstance(key, str):
        raise ValueError("S3 이벤트에 bucket/key가 없습니다.")

    key = unquote_plus(key)
    document_id = _document_id_from_key(key)
    event_id = hashlib.sha256(f"{bucket}:{key}".encode("utf-8")).hexdigest()[:24]
    return {
        "requestId": f"s3:{event_id}",
        "documentId": document_id,
        "userId": _user_id_from_key(key),
        "documentType": "RECEIPT",
        "s3Bucket": bucket,
        "s3Key": key,
        "contentType": object_data.get("contentType"),
    }


def _requests_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("documentType") == "RECEIPT":
        return [_validate_request(payload)]

    records = payload.get("Records")
    if isinstance(records, list) and records:
        requests = []
        for record in records:
            if not isinstance(record, dict) or record.get("eventSource") != "aws:s3":
                raise ValueError("영수증 SQS 메시지에 S3 이벤트가 없습니다.")
            requests.append(_validate_request(_request_from_s3_record(record)))
        return requests

    raise ValueError("영수증 요청 또는 S3 이벤트 payload가 아닙니다.")


def _store_raw_result(
    payload: dict[str, Any],
    raw_ocr_text: str | None,
    input_bucket: str,
    document_id: str,
) -> dict[str, str]:
    bucket = RAW_RESULT_BUCKET or input_bucket
    key = f"{RAW_RESULT_PREFIX}/{document_id}/result.json" if RAW_RESULT_PREFIX else f"{document_id}/result.json"
    raw_payload = dict(payload)
    if raw_ocr_text:
        raw_payload["rawOcrText"] = raw_ocr_text

    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(raw_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    print(f"[info] 영수증 원본 결과 JSON 저장 완료: bucket={bucket}, key={key}")
    return {"bucket": bucket, "key": key}


def _send_result(payload: dict[str, Any]) -> None:
    if not RESULT_QUEUE_URL:
        raise RuntimeError("RESULT_QUEUE_URL이 설정되지 않았습니다.")

    request: dict[str, Any] = {
        "QueueUrl": RESULT_QUEUE_URL,
        "MessageBody": json.dumps(payload, ensure_ascii=False),
    }
    if RESULT_QUEUE_URL.endswith(".fifo"):
        request["MessageGroupId"] = "receipt-results"
        request["MessageDeduplicationId"] = str(payload["requestId"])

    response = _sqs_client().send_message(**request)
    print(
        f"[info] 영수증 결과 SQS 발행 완료: requestId={payload['requestId']}, "
        f"status={payload['status']}, messageId={response.get('MessageId')}"
    )


def _failure_code(error: ReceiptDocumentError) -> str:
    message = str(error)
    if "텍스트" in message or "Tesseract" in message:
        return "OCR_EMPTY_RESULT"
    return "RECEIPT_PARSE_FAILED"


def _process_request(message: dict[str, Any]) -> None:
    request = _validate_request(message)
    request_id = request["requestId"]
    document_id = request["documentId"]
    bucket = request["s3Bucket"]
    key = request["s3Key"]
    user_id = request.get("userId")
    print(f"[info] 영수증 OCR 시작: requestId={request_id}, bucket={bucket}, key={key}")

    image_bytes = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    print(f"[info] 영수증 이미지 다운로드 완료: requestId={request_id}, bytes={len(image_bytes)}")

    raw_ocr_text = None
    try:
        analysis = analyze_receipt_image_bytes(image_bytes, language=OCR_LANGUAGE)
        result = analysis["result"]
        raw_ocr_text = analysis.get("rawOcrText")
        payload: dict[str, Any] = {
            "requestId": request_id,
            "documentId": document_id,
            "userId": user_id,
            "documentType": "RECEIPT",
            "status": "COMPLETED",
            "result": result,
            "warnings": result.get("warnings", []),
            "processedAt": _now(),
        }
    except ReceiptDocumentError as error:
        payload = {
            "requestId": request_id,
            "documentId": document_id,
            "userId": user_id,
            "documentType": "RECEIPT",
            "status": "FAILED",
            "errorCode": _failure_code(error),
            "error": str(error),
            "warnings": [],
            "processedAt": _now(),
        }

    payload["rawResult"] = _store_raw_result(
        payload,
        raw_ocr_text,
        bucket,
        document_id,
    )
    _send_result(payload)
    print(f"[info] 영수증 OCR 완료: requestId={request_id}, status={payload['status']}")


def _is_direct_s3_event(event: dict[str, Any]) -> bool:
    records = event.get("Records")
    return bool(
        isinstance(records, list)
        and records
        and isinstance(records[0], dict)
        and records[0].get("eventSource") == "aws:s3"
    )


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Handle SQS-wrapped S3 events, direct S3 events, or explicit requests."""
    del context

    if _is_direct_s3_event(event):
        for request in _requests_from_payload(event):
            _process_request(request)
        return {}

    failures: list[dict[str, str]] = []
    processed = 0
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "?"))
        try:
            body = record.get("body")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("SQS body가 비어 있습니다.")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("SQS body는 JSON 객체여야 합니다.")
            for request in _requests_from_payload(payload):
                _process_request(request)
            processed += 1
        except Exception as error:
            print(
                f"[error] 영수증 OCR 처리 실패: messageId={message_id}, "
                f"errorType={type(error).__name__}, error={error}"
            )
            failures.append({"itemIdentifier": message_id})

    print(f"[info] 영수증 OCR 배치 완료: processed={processed}, failed={len(failures)}")
    return {"batchItemFailures": failures}
