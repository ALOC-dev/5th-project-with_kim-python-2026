"""
AWS Lambda 핸들러 — 등기부등본 PDF 자동 분석 (멀티 문서: 건물+토지 지원 버전)

트리거: SQS (Spring Boot 백엔드가 DB 저장 + S3 업로드 성공 후 발행한 메시지)

동작:
    1. SQS 메시지 body 파싱 (백엔드 AnalysisRequestMessage 그대로)
    2. sources의 PDF들을 각각 S3에서 다운로드 → 텍스트 추출 → 문서별 파싱
       (표제부 기준 집합건물/일반건물/토지 자동 분류, docHint와 다르면 노트)
    3. 문서 병합 분석 (analyzer.analyze_property):
       - 토지·건물 공동담보 근저당 dedup, 건물·토지 소유자 교차 검증
       - 필수 등기부 누락 시 analysis_status = "NEEDS_MORE_DOCS"
    4. 원본 결과 JSON을 S3에 저장
    5. 결과 SQS(RESULT_QUEUE_URL)로 발행

메시지 스키마 (camelCase — 신규 멀티 문서 형식):
    {
        "submissionId": "sub_a1b2c3d4...",
        "propertyType": "APARTMENT | ROW_HOUSE | MULTI_FAMILY | OFFICETEL
                         | SINGLE_FAMILY | MULTI_HOUSEHOLD",   # 선택
        "leaseType": "JEONSE | WOLSE",                         # 선택, 없으면 JEONSE
        "sources": [
            {"bucket": "aloc-sibang", "key": "uploads/sub_..._building.pdf",
             "docHint": "BUILDING"},                            # docHint 선택
            {"bucket": "aloc-sibang", "key": "uploads/sub_..._land.pdf",
             "docHint": "LAND"}
        ],
        "contract": {
            "owner": "홍길동",
            "address": "서울특별시 동대문구 전농동 152-73",
            "roadAddress": "서울특별시 동대문구 서울시립대로 112-1",
            "deposit": 130000000,
            "price": 150000000,
            "publicPrice": 147000000,
            "seniorTenantDeposits": 80000000     # 다가구 선순위 임차보증금 합계 (선택)
        }
    }

    하위 호환: 기존 단일 "source" 필드도 계속 지원한다.
        {"submissionId": ..., "source": {"bucket": ..., "key": ...}, "contract": {...}}

결과 SQS payload:
    {
        "submissionId": "...",
        "analysis": { ... AnalysisResult.to_dict() (snake_case) ... },
        "rawResult": {"bucket": ..., "key": ...}
    }

주민등록초본 주소 추출 요청:
    {
        "messageType": "RESIDENCE_ADDRESS_EXTRACTION",
        "userId": 7,
        "source": {"bucket": "aloc-sibang", "key": "resident-registration/7/transcript.pdf"}
    }

주민등록초본 주소 추출 결과:
    {
        "messageType": "RESIDENCE_ADDRESS_EXTRACTION",
        "userId": 7,
        "status": "COMPLETED",
        "addresses": [{
            "rawAddress": "서울특별시 동대문구 회기동 62-8",
            "roadAddress": null,
            "jibunAddress": "서울특별시 동대문구 회기동 62-8",
            "current": true,
            "residenceYears": ["2007", "2009"]
        }]
    }

    ★ 백엔드 처리 가이드 (analysis 내부의 기계 판독용 필드):
        analysis.analysis_status == "NEEDS_MORE_DOCS" 이면:
          - analysis.required_documents        예: ["LAND"]  (BUILDING / LAND / COLLECTIVE)
          - analysis.required_documents_reason 사용자에게 보여줄 사유 문자열
          - analysis.address_matches_submission / analysis.address_match_basis 주소 대조 결과
          - 이 경우 risk_level은 "UNKNOWN"으로 온다 (반쪽 데이터로 SAFE 확정 방지).
          → submission 상태를 '추가 서류 필요'로 저장하고 사용자에게 업로드 요청 알림.
          → 사용자가 추가 PDF를 올리면, 기존 PDF + 새 PDF를 모두 sources에 담아
            분석 요청 메시지를 재발행한다 (Lambda는 stateless — 이전 분석을 기억하지 않음).

배포 구성:
    - 이 파일과 analyzer.py를 함께 패키징한다.
    - pdfplumber는 C 확장 의존성이 있으므로 Lambda Layer 또는 컨테이너 이미지 권장.
    - 핸들러 지정: lambda_handler.lambda_handler
    - 이벤트 소스 매핑에서 FunctionResponseTypes: ["ReportBatchItemFailures"] 필수

환경변수:
    RESULT_QUEUE_URL   Lambda가 분석 결과를 발행할 SQS 큐 URL (기본: sibang-result)
    RAW_RESULT_BUCKET  원본 결과 JSON 저장 버킷 (선택, 기본: 첫 번째 입력 PDF 버킷)
    RAW_RESULT_PREFIX  원본 결과 JSON 저장 prefix (선택, 기본 analysis-results)
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from analyzer import (
    DeedDocument,
    analyze_property,
    extract_text_from_pdf_bytes,
    parse_deed_document,
)
from residence_address_extractor import (
    extract_residence_addresses,
    extract_residence_text_from_pdf_bytes,
)

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

RESULT_QUEUE_URL = os.environ.get(
    "RESULT_QUEUE_URL",
    "https://sqs.ap-northeast-2.amazonaws.com/916923735483/sibang-result",
)
RAW_RESULT_BUCKET = os.environ.get("RAW_RESULT_BUCKET")
RAW_RESULT_PREFIX = os.environ.get("RAW_RESULT_PREFIX", "analysis-results").strip("/")


def _store_raw_result_to_s3(payload: dict[str, Any], default_bucket: str) -> dict[str, str]:
    """원본 분석 결과 payload를 S3에 보관한다."""
    bucket = RAW_RESULT_BUCKET or default_bucket
    filename = f"{payload['submissionId']}.json"
    key = f"{RAW_RESULT_PREFIX}/{filename}" if RAW_RESULT_PREFIX else filename
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    print(f"[info] 원본 결과 JSON S3 저장 성공: s3://{bucket}/{key}")
    return {"bucket": bucket, "key": key}


def _send_result_to_sqs(payload: dict[str, Any]) -> None:
    """분석 결과를 Spring Boot가 소비하는 결과 SQS로 발행한다."""
    if not RESULT_QUEUE_URL:
        raise RuntimeError("RESULT_QUEUE_URL 미설정 — 분석 결과 발행 불가")

    body = json.dumps(payload, ensure_ascii=False)
    request = {
        "QueueUrl": RESULT_QUEUE_URL,
        "MessageBody": body,
    }
    if RESULT_QUEUE_URL.endswith(".fifo"):
        identifier = str(payload.get("submissionId") or payload.get("userId"))
        request["MessageGroupId"] = "analysis-results"
        request["MessageDeduplicationId"] = (
            f"{payload.get('messageType', 'REGISTRY_ANALYSIS')}:{identifier}"
        )

    response = sqs.send_message(**request)
    identifier = payload.get("submissionId") or f"userId={payload.get('userId')}"
    print(f"[info] 결과 SQS 발행 성공 (messageId={response.get('MessageId')}, "
          f"identifier={identifier})")


def _resolve_sources(message: dict[str, Any]) -> list[dict[str, Any]]:
    """
    신규 'sources'(배열)와 기존 'source'(단수)를 모두 지원한다.
    """
    sources = message.get("sources")
    if sources:
        return list(sources)
    single = message.get("source")
    if single:
        return [single]
    raise ValueError("메시지에 sources/source가 없습니다 — 분석할 PDF 위치 불명")


def _load_and_parse_documents(
    sources: list[dict[str, Any]], submission_id: str
) -> list[DeedDocument]:
    """
    각 source의 PDF를 다운로드해 문서별로 파싱한다.
    docHint(백엔드/사용자가 지정한 종류)와 표제부 자동 분류가 다르면 노트를 남긴다
    — 사용자가 파일을 뒤바꿔 올린 경우를 감지하기 위함이다.
    """
    documents: list[DeedDocument] = []
    for src in sources:
        bucket, key = src["bucket"], src["key"]
        hint = (src.get("docHint") or "").upper() or None

        obj = s3.get_object(Bucket=bucket, Key=key)
        pdf_bytes = obj["Body"].read()
        text = extract_text_from_pdf_bytes(pdf_bytes)
        doc = parse_deed_document(text)

        if hint and doc.doc_type != "UNKNOWN" and doc.doc_type != hint:
            doc.notes.append(
                f"업로드 지정 종류({hint})와 실제 등기부 종류({doc.doc_type})가 다릅니다. "
                f"파일이 뒤바뀌었을 수 있으니 확인하세요. (s3://{bucket}/{key})"
            )
        print(f"[info] 문서 파싱 완료: submissionId={submission_id}, "
              f"s3://{bucket}/{key}, doc_type={doc.doc_type}, hint={hint}")
        documents.append(doc)
    return documents


def _process_residence_address_extraction(message: dict[str, Any]) -> None:
    """S3의 주민등록초본 PDF에서 주소와 연도만 추출해 결과 큐로 보낸다."""
    user_id = message.get("userId")
    if user_id is None:
        raise ValueError("초본 주소 추출 메시지에 userId가 없습니다.")

    source = message.get("source")
    if not source or not source.get("bucket") or not source.get("key"):
        raise ValueError("초본 주소 추출 메시지에 source.bucket/key가 없습니다.")

    bucket, key = source["bucket"], source["key"]
    print(f"[info] 초본 주소 추출 시작: userId={user_id}, s3://{bucket}/{key}")

    obj = s3.get_object(Bucket=bucket, Key=key)
    pdf_bytes = obj["Body"].read()
    print(f"[info] 초본 PDF 다운로드 완료: userId={user_id}, bytes={len(pdf_bytes)}")
    try:
        text = extract_residence_text_from_pdf_bytes(pdf_bytes)
        print(f"[info] 초본 텍스트 추출 완료: userId={user_id}, chars={len(text)}")
        addresses = extract_residence_addresses(text)
        print(f"[info] 초본 주소 집계 완료: userId={user_id}, addresses={len(addresses)}건")
        payload = {
            "messageType": "RESIDENCE_ADDRESS_EXTRACTION",
            "userId": user_id,
            "status": "COMPLETED",
            "addresses": addresses,
        }
    except ValueError as error:
        payload = {
            "messageType": "RESIDENCE_ADDRESS_EXTRACTION",
            "userId": user_id,
            "status": "FAILED",
            "error": str(error),
            "addresses": [],
        }

    _send_result_to_sqs(payload)
    print(f"[info] 초본 주소 추출 완료: userId={user_id}, "
          f"status={payload['status']}, addresses={len(payload['addresses'])}건")


def _process_message(message: dict[str, Any]) -> None:
    """SQS 메시지 하나(=백엔드가 발행한 AnalysisRequestMessage)를 처리한다."""
    if message.get("messageType") == "RESIDENCE_ADDRESS_EXTRACTION":
        _process_residence_address_extraction(message)
        return

    submission_id = message["submissionId"]
    sources = _resolve_sources(message)
    property_type = message.get("propertyType")
    lease_type = message.get("leaseType")
    contract = message.get("contract") or {}

    owner = contract.get("owner")
    tenant_name = contract.get("tenantName")  # 로그 추적용 (분석엔 미사용)
    submitted_address = contract.get("address")
    road_address = contract.get("roadAddress")

    print(f"[info] 분석 시작: submissionId={submission_id}, "
          f"sources={len(sources)}건, propertyType={property_type}, leaseType={lease_type}, "
          f"owner={owner}, tenantName={tenant_name}, address={submitted_address}")

    documents = _load_and_parse_documents(sources, submission_id)

    result = analyze_property(
        documents,
        property_type=property_type,
        lease_type=lease_type,
        contract_owner=owner,
        submitted_address=submitted_address,
        road_address=road_address,
        deposit=contract.get("deposit"),
        property_price=contract.get("price"),
        public_price=contract.get("publicPrice"),
        # 다가구 선순위 임차보증금 — 등기부에 없는 값이라 백엔드 입력으로 받는다
        senior_tenant_deposits=contract.get("seniorTenantDeposits"),
    )

    payload = {
        "submissionId": submission_id,
        "analysis": result.to_dict(),
    }
    raw_result = _store_raw_result_to_s3(payload, sources[0]["bucket"])
    payload["rawResult"] = raw_result
    _send_result_to_sqs(payload)

    if result.analysis_status == "NEEDS_MORE_DOCS":
        print(f"[warn] 추가 서류 필요: submissionId={submission_id}, "
              f"required={result.required_documents}, "
              f"reason={result.required_documents_reason}")

    print(f"[info] 처리 완료: submissionId={submission_id} "
          f"(status={result.analysis_status}, 등급={result.risk_level}, "
          f"점수={result.risk_score}, HUG={result.hug_eligible}, LH={result.lh_eligible})")


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """
    SQS 이벤트 소스 매핑을 통해 호출된다. ReportBatchItemFailures를 사용하므로,
    실패한 메시지의 messageId만 batchItemFailures에 담아 반환한다.
    """
    records = event.get("Records", [])
    processed = 0
    batch_item_failures: list[dict[str, str]] = []

    for sqs_record in records:
        message_id = sqs_record.get("messageId", "?")
        try:
            message = json.loads(sqs_record["body"])
            _process_message(message)
            processed += 1
        except Exception as e:
            print(f"[error] 처리 실패 messageId={message_id}: {e}")
            batch_item_failures.append({"itemIdentifier": message_id})

    summary = {"processed": processed, "failed": len(batch_item_failures)}
    print(f"[info] 완료: {summary}")

    return {"batchItemFailures": batch_item_failures}
