import importlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from receipt_ocr import ReceiptDocumentError


RESULT_QUEUE_URL = "https://sqs.ap-northeast-2.amazonaws.com/123/receipt-result"


def sqs_record(message_id, body):
    return {
        "messageId": message_id,
        "body": json.dumps(body, ensure_ascii=False),
    }


def receipt_request():
    return {
        "requestId": "req_001",
        "documentId": "doc_1001",
        "userId": 4,
        "documentType": "RECEIPT",
        "s3Bucket": "aloc-sibang",
        "s3Key": "receipts/4/doc_1001/original.jpg",
        "contentType": "image/jpeg",
    }


def s3_notification():
    return {
        "Records": [{
            "eventSource": "aws:s3",
            "eventName": "ObjectCreated:Put",
            "s3": {
                "bucket": {"name": "aloc-sibang"},
                "object": {
                    "key": "receipts/4/doc_1001/original.jpg",
                },
            },
        }]
    }


class ReceiptLambdaHandlerTest(unittest.TestCase):
    def setUp(self):
        self.s3 = MagicMock()
        self.sqs = MagicMock()
        self.s3.get_object.return_value = {
            "Body": SimpleNamespace(read=MagicMock(return_value=b"image-bytes"))
        }
        self.s3.put_object.return_value = {}
        self.sqs.send_message.return_value = {"MessageId": "result-1"}

        sys.modules.pop("lambda_handler", None)
        with patch.dict(
            os.environ,
            {
                "RESULT_QUEUE_URL": RESULT_QUEUE_URL,
                "RAW_RESULT_PREFIX": "ocr-results/RECEIPT",
                "OCR_LANGUAGE": "kor+eng",
            },
        ):
            self.handler = importlib.import_module("lambda_handler")
        self.handler.s3 = self.s3
        self.handler.sqs = self.sqs

    def test_processes_request_message_and_publishes_completed_result(self):
        analysis = {
            "result": {
                "merchantName": "카페 파도",
                "totalAmount": 8000,
                "warnings": [],
            },
            "rawOcrText": "카페 파도\n합계 8,000원",
        }
        self.handler.analyze_receipt_image_bytes = MagicMock(return_value=analysis)

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", receipt_request())]},
            None,
        )

        self.assertEqual(response, {"batchItemFailures": []})
        self.s3.get_object.assert_called_once_with(
            Bucket="aloc-sibang",
            Key="receipts/4/doc_1001/original.jpg",
        )
        self.s3.put_object.assert_called_once()
        result_payload = json.loads(
            self.sqs.send_message.call_args.kwargs["MessageBody"]
        )
        self.assertEqual(result_payload["documentType"], "RECEIPT")
        self.assertEqual(result_payload["status"], "COMPLETED")
        self.assertEqual(result_payload["requestId"], "req_001")
        self.assertEqual(result_payload["result"]["totalAmount"], 8000)

    def test_processes_s3_notification_body_without_spring_request_message(self):
        analysis = {
            "result": {"merchantName": "카페 파도", "totalAmount": 8000, "warnings": []},
            "rawOcrText": "카페 파도\n합계 8,000원",
        }
        self.handler.analyze_receipt_image_bytes = MagicMock(return_value=analysis)

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", s3_notification())]},
            None,
        )

        self.assertEqual(response, {"batchItemFailures": []})
        self.s3.get_object.assert_called_once_with(
            Bucket="aloc-sibang",
            Key="receipts/4/doc_1001/original.jpg",
        )

    def test_publishes_failed_result_for_receipt_content_error(self):
        self.handler.analyze_receipt_image_bytes = MagicMock(
            side_effect=ReceiptDocumentError("영수증 총액을 확인하지 못했습니다.")
        )

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", receipt_request())]},
            None,
        )

        self.assertEqual(response, {"batchItemFailures": []})
        payload = json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["errorCode"], "RECEIPT_PARSE_FAILED")

    def test_returns_partial_failure_for_s3_error(self):
        self.s3.get_object.side_effect = RuntimeError("s3 unavailable")

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", receipt_request())]},
            None,
        )

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )
        self.sqs.send_message.assert_not_called()

    def test_returns_partial_failure_when_result_publish_fails(self):
        analysis = {
            "result": {"merchantName": "카페 파도", "totalAmount": 8000, "warnings": []},
            "rawOcrText": "카페 파도\n합계 8,000원",
        }
        self.handler.analyze_receipt_image_bytes = MagicMock(return_value=analysis)
        self.sqs.send_message.side_effect = RuntimeError("sqs unavailable")

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", receipt_request())]},
            None,
        )

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )

    def test_rejects_non_receipt_request(self):
        request = receipt_request()
        request["documentType"] = "REGISTRY_ANALYSIS"

        response = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", request)]},
            None,
        )

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )
        self.s3.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
