"""Runs the handler against moto's in-memory ECS instead of hand-written responses."""
import boto3
import pytest
from moto import mock_aws

import handler


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CLUSTER_NAME", "c1")
    monkeypatch.setenv("SERVICE_NAME", "s1")
    monkeypatch.setenv("COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


@mock_aws
def test_new_service_is_not_restarted_while_its_first_deployment_rolls_out():
    # moto reports a freshly created service as IN_PROGRESS, which is exactly the
    # situation the guard exists for. The restart call itself is covered in test_handler.py.
    ecs = boto3.client("ecs")
    ecs.create_cluster(clusterName="c1")
    ecs.register_task_definition(
        family="td",
        containerDefinitions=[{"name": "web", "image": "nginx", "memory": 128}],
    )
    ecs.create_service(cluster="c1", serviceName="s1", taskDefinition="td", desiredCount=1)

    event = {"detail": {"alarmName": "a", "state": {"value": "ALARM"}}}
    result = handler.lambda_handler(event)
    assert result["action"] == "skipped"
    assert "in progress" in result["reason"]
