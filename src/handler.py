"""Restart an ECS service when a CloudWatch alarm goes into ALARM.

Flow: CloudWatch alarm -> EventBridge rule -> this function -> ecs:UpdateService
with forceNewDeployment, which replaces the tasks one batch at a time.

Guards, so a bad alarm cannot cause a restart loop:
  * only the ALARM state acts; OK and INSUFFICIENT_DATA are ignored
  * nothing happens while a deployment is already rolling out
  * nothing happens if the current deployment is newer than COOLDOWN_SECONDS
  * DRY_RUN=true logs the decision and changes nothing
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def _log(**fields):
    logger.info(json.dumps(fields, default=str))


def _result(action, **fields):
    _log(action=action, **fields)
    return {"action": action, **fields}


def lambda_handler(event, context=None, client=None, now=None):
    cluster = os.environ["CLUSTER_NAME"]
    service = os.environ["SERVICE_NAME"]
    cooldown = int(os.getenv("COOLDOWN_SECONDS", "600"))
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    detail = event.get("detail", {})
    alarm = detail.get("alarmName", "unknown")
    state = detail.get("state", {}).get("value")
    if state != "ALARM":
        return _result("ignored", reason=f"alarm state is {state}", alarm=alarm)

    ecs = client or boto3.client("ecs")
    now = now or datetime.now(timezone.utc)

    response = ecs.describe_services(cluster=cluster, services=[service])
    if response.get("failures") or not response.get("services"):
        # Raise so the failure shows up in Lambda errors and can be alerted on.
        raise RuntimeError(f"service {service} not found in cluster {cluster}: {response.get('failures')}")

    svc = response["services"][0]
    if svc.get("status") != "ACTIVE":
        return _result("skipped", reason=f"service status is {svc.get('status')}", alarm=alarm)

    primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
    if primary is None:
        return _result("skipped", reason="no primary deployment found", alarm=alarm)

    if primary.get("rolloutState") == "IN_PROGRESS":
        return _result("skipped", reason="a deployment is already in progress", alarm=alarm)

    age = (now - primary["createdAt"]).total_seconds()
    if age < cooldown:
        return _result("skipped", reason=f"cooldown: last deployment {int(age)}s ago, wait {cooldown}s", alarm=alarm)

    if dry_run:
        return _result("would_restart", reason="DRY_RUN is true", alarm=alarm, cluster=cluster, service=service)

    ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
    return _result("restarted", alarm=alarm, cluster=cluster, service=service)
