"""Tests for infra/monitoring.yaml — the monitoring templates.

Scenario requirement: "Basic monitoring and alerting for application health".

Command: python -m pytest tests/test_monitoring_template.py -q
"""
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "monitoring.yaml"
CLOUDFRONT_TEMPLATE_PATH = ROOT / "infra" / "monitoring-cloudfront.yaml"
STORAGE_PATH = ROOT / "infra" / "storage.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_intrinsic(loader, tag_suffix, node):
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node)
    else:
        raise TypeError(f"Unsupported CloudFormation node: {type(node).__name__}")
    return {f"!{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


def _template(path=TEMPLATE_PATH):
    with path.open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=CloudFormationLoader)


def _alarms():
    template = _template()
    return {
        name: resource["Properties"]
        for name, resource in template["Resources"].items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }


def test_alarms_exist_for_application_health():
    """The brief requires monitoring for application health."""
    alarms = _alarms()
    assert len(alarms) >= 4, "expected a set of alarms covering health, latency and errors"
    namespaces = {p["Namespace"] for p in alarms.values()}
    assert "AWS/ApplicationELB" in namespaces
    assert "AWS/S3" in namespaces
    assert "AWS/CloudFront" not in namespaces


def test_monitoring_stacks_separate_cloudfront_from_regional_metrics():
    regional = _template()
    cloudfront = _template(CLOUDFRONT_TEMPLATE_PATH)
    regional_namespaces = {
        resource["Properties"].get("Namespace")
        for resource in regional["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    cloudfront_namespaces = {
        resource["Properties"].get("Namespace")
        for resource in cloudfront["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    regional_dashboard = regional["Resources"]["CloudWatchDashboard"]["Properties"]["DashboardBody"]["!Sub"]
    cloudfront_dashboard = cloudfront["Resources"]["CloudWatchDashboard"]["Properties"]["DashboardBody"]["!Sub"]

    assert "AWS/CloudFront" not in regional_namespaces
    assert "AWS/CloudFront" not in regional_dashboard
    assert regional_namespaces <= {"AWS/ApplicationELB", "AWS/S3", "AWS/DynamoDB"}
    assert cloudfront_namespaces == {"AWS/CloudFront"}
    assert "AWS/CloudFront" in cloudfront_dashboard
    assert "AWS/ApplicationELB" not in cloudfront_dashboard
    assert "AWS/S3" not in cloudfront_dashboard
    assert "AWS/DynamoDB" not in cloudfront_dashboard


def test_alb_dimensions_use_metric_suffix_parameters_not_full_arns():
    template = _template()
    dimensions = [
        dimension
        for alarm in _alarms().values()
        if alarm["Namespace"] == "AWS/ApplicationELB"
        for dimension in alarm["Dimensions"]
    ]
    dimension_names = {dimension["Name"] for dimension in dimensions}
    assert {"LoadBalancer", "TargetGroup"} <= dimension_names
    assert all("arn:" not in str(dimension["Value"]) for dimension in dimensions)
    assert {"!Ref": "AlbDimensionValue"} in [d["Value"] for d in dimensions]
    assert {"!Ref": "TargetGroupDimensionValue"} in [d["Value"] for d in dimensions]
    assert "AlbDimensionValue" in template["Parameters"]
    assert "TargetGroupDimensionValue" in template["Parameters"]
    deploy_script = (ROOT / "ci" / "deploy.sh").read_text(encoding="utf-8")
    assert 'ALB_DIMENSION="${ALB_ARN#*:loadbalancer/}"' in deploy_script
    assert 'TG_DIMENSION="${TG_ARN##*:}"' in deploy_script
    assert 'TG_DIMENSION="${TG_ARN#*:targetgroup/}"' not in deploy_script


def test_s3_request_alarm_filter_matches_bucket_request_metrics():
    alarm = _alarms()["StorageErrorAlarm"]
    dimensions = {dimension["Name"]: dimension["Value"] for dimension in alarm["Dimensions"]}

    storage = _template(STORAGE_PATH)
    configuration = storage["Resources"]["ImagesBucket"]["Properties"]["MetricsConfigurations"][0]
    assert dimensions["FilterId"] == configuration["Id"]
    assert configuration["Prefix"] == ""


def test_unhealthy_host_count_alarm_exists_and_is_the_primary_signal():
    """If the ALB cannot reach the app, this must fire."""
    names = list(_alarms())
    assert any("unhealthy" in n.lower() for n in names)
    unhealthy = next(p for n, p in _alarms().items() if "unhealthy" in n.lower())
    assert unhealthy["MetricName"] == "UnHealthyHostCount"
    assert unhealthy["ComparisonOperator"] == "GreaterThanThreshold"
    # Fires only when there is at least one unhealthy target.
    assert unhealthy["Threshold"] == 0
    assert unhealthy["TreatMissingData"] == "notBreaching"


def test_every_alarm_has_dimensions_period_and_breach_policy():
    """An alarm with no dimensions can never resolve to data in CloudWatch."""
    for name, props in _alarms().items():
        assert props.get("Dimensions"), f"{name} has no dimensions"
        assert props.get("Period"), f"{name} has no period"
        assert props.get("EvaluationPeriods"), f"{name} has no evaluation period"
        assert props.get("ComparisonOperator"), f"{name} has no comparison operator"
        assert props.get("TreatMissingData") == "notBreaching", f"{name} alarm state is ambiguous"


def test_alarms_are_metric_only_and_never_publish_to_sns():
    """An unconfirmed SNS subscription would look configured while paging nobody.

    In a throwaway sandbox nobody clicks the confirmation link, so a topic-backed
    alarm silently never notifies. Metric-only alarms always exist and can be
    read by the verification agent.
    """
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "AWS::SNS::Topic" not in source
    assert "AWS::SNS::Subscription" not in source
    for name, props in _alarms().items():
        assert "AlarmActions" not in props, f"{name} publishes to a notification target"
        assert "OKActions" not in props, f"{name} publishes on OK"


def test_alarm_names_are_stack_scoped_not_hardcoded():
    """Alarm names must derive from the stack so a redeploy updates rather than
    accumulating duplicates."""
    for name, props in _alarms().items():
        assert isinstance(props["AlarmName"], dict), f"{name} AlarmName is not intrinsic"
        assert "!Sub" in props["AlarmName"], f"{name} AlarmName is not stack-scoped"
        assert "${AWS::StackName}" in props["AlarmName"]["!Sub"], (
            f"{name} AlarmName does not derive from the stack name"
        )


def test_monitoring_creates_no_billing_impacting_resources():
    """Alarms and a dashboard are free; anything else would add cost to a
    cost-conscious poc."""
    types = {r["Type"] for r in _template()["Resources"].values()}
    assert types <= {
        "AWS::CloudWatch::Alarm",
        "AWS::CloudWatch::Dashboard",
        "AWS::CloudFormation::WaitConditionHandle",
    }, f"unexpected resource types in a monitoring stack: {types}"


def test_dashboard_is_valid_json_with_widgets():
    """CloudWatch rejects the whole dashboard if DashboardBody is not valid JSON."""
    import json

    template = _template()
    dashboard = template["Resources"]["CloudWatchDashboard"]["Properties"]
    # The body is a !Sub over the parameters; substitute them to check the shape.
    body = dashboard["DashboardBody"]["!Sub"]
    rendered = (
        body.replace("${AWS::Region}", "eu-west-1")
        .replace("${AWS::StackName}", "community-board-monitoring")
        .replace("${AlbDimensionName}", "LoadBalancer")
        .replace("${TargetGroupDimension}", "TargetGroup")
        .replace("${AlbDimensionValue}", "app/x/y")
        .replace("${TargetGroupDimensionValue}", "targetgroup/x/y")
        .replace("${ImagesBucketName}", "images-test")
        .replace("${EventsTableName}", "events-test")
    )
    parsed = json.loads(rendered)
    assert "widgets" in parsed
    assert len(parsed["widgets"]) >= 4
    assert dashboard["DashboardName"] == {"!Sub": "${AWS::StackName}-overview"}


def test_cloudfront_template_contains_alarm_and_dashboard_in_global_metric_region():
    template = _template(CLOUDFRONT_TEMPLATE_PATH)
    resource_types = {resource["Type"] for resource in template["Resources"].values()}
    alarms = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    ]
    dashboard = template["Resources"]["CloudWatchDashboard"]["Properties"]["DashboardBody"]["!Sub"]

    assert resource_types <= {"AWS::CloudWatch::Alarm", "AWS::CloudWatch::Dashboard"}
    assert alarms
    assert all(alarm["Namespace"] == "AWS/CloudFront" for alarm in alarms)
    assert all(
        {dimension["Name"]: dimension["Value"] for dimension in alarm["Dimensions"]}.get("Region") == "Global"
        for alarm in alarms
    )
    assert '"region": "us-east-1"' in dashboard
    assert '"AWS/CloudFront"' in dashboard


def test_latency_alarm_does_not_claim_the_concurrency_target():
    """The alarm is a signal, not a capacity claim.

    NFR3 keeps the 500-concurrent-user target explicitly unproven, so the
    description must not imply the system has been load tested.
    """
    latency = next(p for n, p in _alarms().items() if n.endswith("LatencyAlarm"))
    description = latency["AlarmDescription"].lower()
    assert "no load test" in description
    assert "unproven" in description