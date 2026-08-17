import importlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from residence_address_extractor import ResidenceDocumentError


RESULT_QUEUE_URL = (
    "https://sqs.ap-northeast-2.amazonaws.com/916923735483/sibang-result"
)


def sqs_record(message_id, body):
    return {
        "messageId": message_id,
        "body": json.dumps(body, ensure_ascii=False),
    }


def valid_request(user_id=4):
    return {
        "messageType": "RESIDENCE_ADDRESS_EXTRACTION",
        "userId": user_id,
        "source": {
            "bucket": "aloc-sibang",
            "key": f"resident-registration/{user_id}/transcript.pdf",
        },
    }


class LambdaHandlerTest(unittest.TestCase):
    def setUp(self):
        self.s3 = MagicMock()
        self.sqs = MagicMock()
        self.s3.get_object.return_value = {
            "Body": SimpleNamespace(read=MagicMock(return_value=b"pdf-bytes"))
        }
        self.sqs.send_message.return_value = {"MessageId": "result-message"}

        def client(service_name):
            return {"s3": self.s3, "sqs": self.sqs}[service_name]

        fake_boto3 = SimpleNamespace(client=MagicMock(side_effect=client))
        sys.modules.pop("lambda_handler", None)
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            with patch.dict(
                os.environ,
                {
                    "RESULT_QUEUE_URL": RESULT_QUEUE_URL,
                    "OCR_LANGUAGE": "kor+eng",
                    "OCR_DPI": "300",
                },
            ):
                self.handler = importlib.import_module("lambda_handler")

    def test_publishes_completed_result_for_valid_request(self):
        addresses = [
            {
                "rawAddress": "경기도 하남시 신장동 467-5",
                "roadAddress": None,
                "jibunAddress": "경기도 하남시 신장동 467-5",
                "current": True,
                "residenceYears": ["2007"],
            }
        ]
        self.handler.extract_residence_text_from_pdf_bytes = MagicMock(
            return_value="ocr text"
        )
        self.handler.extract_residence_addresses = MagicMock(return_value=addresses)

        result = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", valid_request())]},
            None,
        )

        self.assertEqual(result, {"batchItemFailures": []})
        self.s3.get_object.assert_called_once_with(
            Bucket="aloc-sibang",
            Key="resident-registration/4/transcript.pdf",
        )
        self.handler.extract_residence_text_from_pdf_bytes.assert_called_once_with(
            b"pdf-bytes",
            language="kor+eng",
            dpi=300,
        )
        payload = json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(payload["messageType"], "RESIDENCE_ADDRESS_EXTRACTION")
        self.assertEqual(payload["userId"], 4)
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["addresses"], addresses)

    def test_publishes_failed_result_for_document_content_error(self):
        self.handler.extract_residence_text_from_pdf_bytes = MagicMock(
            side_effect=ResidenceDocumentError("주소와 변동 연도를 찾지 못했습니다.")
        )

        result = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", valid_request())]},
            None,
        )

        self.assertEqual(result, {"batchItemFailures": []})
        payload = json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["userId"], 4)
        self.assertEqual(payload["addresses"], [])
        self.assertIn("주소와 변동 연도", payload["error"])

    def test_returns_partial_batch_failure_for_s3_error(self):
        self.s3.get_object.side_effect = RuntimeError("s3 unavailable")

        result = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", valid_request())]},
            None,
        )

        self.assertEqual(
            result,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )
        self.sqs.send_message.assert_not_called()

    def test_returns_partial_batch_failure_when_result_publish_fails(self):
        self.handler.extract_residence_text_from_pdf_bytes = MagicMock(
            return_value="ocr text"
        )
        self.handler.extract_residence_addresses = MagicMock(return_value=[])
        self.sqs.send_message.side_effect = RuntimeError("sqs unavailable")

        result = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", valid_request())]},
            None,
        )

        self.assertEqual(
            result,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )

    def test_rejects_wrong_message_type_without_downloading_pdf(self):
        request = valid_request()
        request["messageType"] = "REGISTRY_ANALYSIS"

        result = self.handler.lambda_handler(
            {"Records": [sqs_record("message-1", request)]},
            None,
        )

        self.assertEqual(
            result,
            {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
        )
        self.s3.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
