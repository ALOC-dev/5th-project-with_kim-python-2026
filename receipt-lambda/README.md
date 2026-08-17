# Receipt OCR Lambda

This Lambda accepts receipt images from S3, extracts structured receipt fields
with Korean and English Tesseract OCR, stores a private raw result JSON in S3,
and publishes a result message to SQS.

## Recommended AWS flow

```text
S3 ObjectCreated (receipts/ prefix)
    -> receipt-request SQS
    -> Lambda SQS event source mapping
    -> S3 image download and OCR
    -> ocr-results/RECEIPT/.../result.json
    -> receipt-result SQS
```

The Lambda also accepts the explicit request message described below and direct
S3 events. The S3-to-SQS flow is recommended because SQS buffers retries and
supports a DLQ.

## S3 key convention

Use this key shape so the S3 event contains enough identity information:

```text
receipts/{userId}/{documentId}/original.jpg
```

The result is written outside the trigger prefix:

```text
ocr-results/RECEIPT/{documentId}/result.json
```

Configure the S3 notification to match only `receipts/`. Keep the bucket
private. Do not attach the notification to `ocr-results/`.

## SQS request messages

S3 can send its native event to `receipt-request`. The handler also supports an
explicit request message for callers that already have a request queue:

```json
{
  "requestId": "req_001",
  "documentId": "doc_1001",
  "userId": 4,
  "documentType": "RECEIPT",
  "s3Bucket": "aloc-sibang",
  "s3Key": "receipts/4/doc_1001/original.jpg",
  "contentType": "image/jpeg"
}
```

## Result message

```json
{
  "requestId": "req_001",
  "documentId": "doc_1001",
  "userId": 4,
  "documentType": "RECEIPT",
  "status": "COMPLETED",
  "result": {
    "merchantName": "카페 파도",
    "businessNumber": "123-45-67890",
    "transactionDate": "2026-08-01",
    "transactionTime": "14:32",
    "supplyAmount": 7273,
    "vat": 727,
    "totalAmount": 8000,
    "paymentMethod": "신용카드",
    "items": [],
    "confidence": 1.0,
    "warnings": []
  },
  "warnings": [],
  "processedAt": "2026-08-01T10:00:08Z",
  "rawResult": {
    "bucket": "aloc-sibang",
    "key": "ocr-results/RECEIPT/doc_1001/result.json"
  }
}
```

Content failures publish `status=FAILED` and do not request an SQS retry.
S3, OCR runtime, result-S3, and result-SQS infrastructure failures return the
failed SQS record in `batchItemFailures` so Lambda retries it.

## Environment variables

```text
RESULT_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/.../receipt-result
RAW_RESULT_BUCKET=aloc-sibang                 # optional; defaults to input bucket
RAW_RESULT_PREFIX=ocr-results/RECEIPT          # optional
OCR_LANGUAGE=kor+eng                           # optional
```

## Local tests

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 -m py_compile lambda_handler.py receipt_ocr.py
```

The unit tests mock AWS and Tesseract. A real OCR run requires the Docker image
because the image includes Tesseract and the Korean language data.

## Test one local photo

Build the image, then mount a local receipt photo as read-only. This path does
not call S3 or SQS and prints only the structured result.

```bash
RECEIPT_IMAGE="/absolute/path/to/receipt.jpg"

docker run --rm --platform linux/amd64 \
  -v "$RECEIPT_IMAGE:/tmp/receipt.jpg:ro" \
  --entrypoint python \
  receipt-ocr-lambda:test \
  /var/task/local_test.py /tmp/receipt.jpg
```

The output contains `merchantName`, transaction date, amounts, items,
confidence, warnings, and `rawOcrCharCount`. The raw OCR text is intentionally
not printed because a receipt can contain personal or payment information.

## Build

Build for the architecture selected by the Lambda function. For an x86_64
function on an Apple Silicon development machine:

```bash
docker build --platform linux/amd64 -t receipt-ocr-lambda:test .
```

Push the image to ECR and configure the Lambda image entry point as:

```text
lambda_handler.lambda_handler
```
