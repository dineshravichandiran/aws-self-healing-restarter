import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

import handler

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
CLUSTER, SERVICE = "demo-cluster", "demo-svc"


def alarm_event(state="ALARM", name="myapp-slo-page-fast"):
    return {"detail": {"alarmName": name, "state": {"value": state}}}


def service_description(status="ACTIVE", rollout="COMPLETED", age_seconds=3600):
    return {
        "services": [
            {
                "serviceName": SERVICE,
                "status": status,
                "deployments": [
                    {
                        "status": "PRIMARY",
                        "rolloutState": rollout,
                        "createdAt": NOW - timedelta(seconds=age_seconds),
                    }
                ],
            }
        ],
        "failures": [],
    }


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CLUSTER_NAME", CLUSTER)
    monkeypatch.setenv("SERVICE_NAME", SERVICE)
    monkeypatch.setenv("COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def ecs():
    client = boto3.client("ecs")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def describe_args():
    return {"cluster": CLUSTER, "services": [SERVICE]}


def test_ok_state_is_ignored_without_calling_aws(ecs):
    client, stubber = ecs  # no responses queued: any AWS call would fail the test
    result = handler.lambda_handler(alarm_event("OK"), client=client, now=NOW)
    assert result["action"] == "ignored"


def test_alarm_restarts_the_service(ecs):
    client, stubber = ecs
    stubber.add_response("describe_services", service_description(), describe_args())
    stubber.add_response(
        "update_service",
        {"service": {"serviceName": SERVICE}},
        {"cluster": CLUSTER, "service": SERVICE, "forceNewDeployment": True},
    )
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "restarted"
    assert result["service"] == SERVICE


def test_dry_run_does_not_update(monkeypatch, ecs):
    monkeypatch.setenv("DRY_RUN", "true")
    client, stubber = ecs
    stubber.add_response("describe_services", service_description(), describe_args())
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "would_restart"


def test_recent_deployment_is_left_alone(ecs):
    client, stubber = ecs
    stubber.add_response("describe_services", service_description(age_seconds=120), describe_args())
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "skipped"
    assert "cooldown" in result["reason"]


def test_deployment_in_progress_is_left_alone(ecs):
    client, stubber = ecs
    stubber.add_response("describe_services", service_description(rollout="IN_PROGRESS"), describe_args())
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "skipped"
    assert "in progress" in result["reason"]


def test_draining_service_is_left_alone(ecs):
    client, stubber = ecs
    stubber.add_response("describe_services", service_description(status="DRAINING"), describe_args())
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "skipped"


def test_missing_service_raises_so_the_error_is_visible(ecs):
    client, stubber = ecs
    stubber.add_response(
        "describe_services",
        {"services": [], "failures": [{"arn": "x", "reason": "MISSING"}]},
        describe_args(),
    )
    with pytest.raises(RuntimeError, match="not found"):
        handler.lambda_handler(alarm_event(), client=client, now=NOW)


def test_sample_event_file_is_valid():
    with open("events/alarm.json") as f:
        event = json.load(f)
    assert event["detail"]["state"]["value"] == "ALARM"


def test_service_without_a_primary_deployment_is_left_alone(ecs):
    client, stubber = ecs
    description = service_description()
    description["services"][0]["deployments"][0]["status"] = "ACTIVE"
    stubber.add_response("describe_services", description, describe_args())
    result = handler.lambda_handler(alarm_event(), client=client, now=NOW)
    assert result["action"] == "skipped"
    assert "primary" in result["reason"]
