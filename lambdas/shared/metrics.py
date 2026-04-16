"""Custom CloudWatch metric emission.

Namespace : CloudDelivery/{env}   (e.g. CloudDelivery/dev)
All metrics include a base Env dimension.
Additional dimensions can be passed via the dimensions dict.

Metric catalogue (INTERFACE_SPEC.md §11.1):
  matching.duration_ms          Milliseconds  Env, Result
  matching.couriers_in_radius   Count         Env
  matching.assignment_result    Count         Env, Result
  matching.retry_count          Count         Env
  location.update_duration_ms   Milliseconds  Env
  location.active_couriers      Count         Env
  api.request_duration_ms       Milliseconds  Env, Endpoint, Method, StatusCode
  notification.delivery_result  Count         Env, Result
"""
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

_cw = boto3.client("cloudwatch", region_name="us-east-1")
_ENV = os.environ.get("ENV", "dev")
_NAMESPACE = f"CloudDelivery/{_ENV}"


def emit(
    metric_name: str,
    value: float,
    unit: str = "Count",
    dimensions: Optional[dict] = None,
) -> None:
    """Emit a single custom CloudWatch metric data point.

    Failures are logged and swallowed — a metrics emission failure must never
    cause the Lambda to return a batch item failure.
    """
    dims = [{"Name": "Env", "Value": _ENV}]
    if dimensions:
        for k, v in dimensions.items():
            dims.append({"Name": k, "Value": str(v)})

    try:
        _cw.put_metric_data(
            Namespace=_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": dims,
                }
            ],
        )
    except ClientError as exc:
        # Import lazily to avoid circular dependency with logger
        import logging

        logging.getLogger(__name__).warning(
            "metrics_emit_failed metric=%s error=%s", metric_name, exc
        )
