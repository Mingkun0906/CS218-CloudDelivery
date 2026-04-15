"""Kinesis record decoding utilities.

Kinesis delivers records with base64-encoded data payloads. These helpers
decode a record into a Python dict and extract the sequence number used
for partial batch failure reporting (ReportBatchItemFailures).
"""
import base64
import json
from typing import Any, Dict


def decode_kinesis_record(record: Dict[str, Any]) -> dict:
    """Decode the base64-encoded data field of a Kinesis record.

    Args:
        record: A single element from event["Records"] in a Kinesis-triggered Lambda.

    Returns:
        Parsed JSON dict of the record's data payload (the EventEnvelope).

    Raises:
        ValueError: if the data field is missing, not valid base64, or not valid JSON.
    """
    try:
        raw_b64 = record["kinesis"]["data"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Missing kinesis.data in record: {exc}") from exc

    try:
        decoded_bytes = base64.b64decode(raw_b64)
    except Exception as exc:
        raise ValueError(f"Invalid base64 in kinesis.data: {exc}") from exc

    try:
        return json.loads(decoded_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"kinesis.data is not valid JSON: {exc}") from exc


def get_sequence_number(record: Dict[str, Any]) -> str:
    """Extract the Kinesis sequence number used as the batch item failure identifier."""
    try:
        return record["kinesis"]["sequenceNumber"]
    except (KeyError, TypeError):
        return record.get("messageId", "unknown")
