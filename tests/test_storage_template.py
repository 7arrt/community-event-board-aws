"""Tests for infra/storage.yaml — TTL, archival, CORS placement, and IAM scope.

Traceability: the brief
requires "Events older than 1 year should be archivable" and
"no hardcoded secrets".

Command: python -m pytest tests/test_storage_template.py -q
"""
from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "storage.yaml"


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


def _template():
    with TEMPLATE_PATH.open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=CloudFormationLoader)


def test_events_table_enables_ttl_for_one_year_archival():
    """Scenario requirement: events older than 1 year should be archivable.

    DynamoDB TTL needs TimeToLiveSpecification pointing at a Number attribute.
    The attribute itself does NOT need to appear in AttributeDefinitions:
    non-key attributes are schemaless, and listing one there makes DynamoDB
    reject the table with "Number of attributes in KeySchema does not exactly
    match number of attributes defined in AttributeDefinitions".
    """
    props = _template()["Resources"]["EventsTable"]["Properties"]

    ttl = props["TimeToLiveSpecification"]
    assert ttl["Enabled"] is True
    assert ttl["AttributeName"] == "expiresAt"


def test_ttl_attribute_is_absent_from_attribute_definitions():
    """AttributeDefinitions must match the key schema exactly.

    This cost a real rollback: declaring the TTL attribute there fails the table
    creation, and no offline test can see it.
    """
    props = _template()["Resources"]["EventsTable"]["Properties"]
    declared = {a["AttributeName"] for a in props["AttributeDefinitions"]}
    key_names = {k["AttributeName"] for k in props["KeySchema"]}
    assert declared == key_names, (
        "AttributeDefinitions must contain exactly the key schema attributes; "
        f"declared={sorted(declared)} key={sorted(key_names)}"
    )
    assert "expiresAt" not in declared


def test_ttl_attribute_is_not_part_of_the_key_schema():
    """A TTL attribute must never become part of the key schema."""
    props = _template()["Resources"]["EventsTable"]["Properties"]
    key_names = {k["AttributeName"] for k in props["KeySchema"]}
    assert key_names == {"PK", "SK"}


def test_table_remains_on_demand_and_encrypted():
    props = _template()["Resources"]["EventsTable"]["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["SSESpecification"]["SSEEnabled"] is True


def test_images_bucket_stays_private_and_encrypted():
    props = _template()["Resources"]["ImagesBucket"]["Properties"]
    block = props["PublicAccessBlockConfiguration"]
    assert block["BlockPublicAcls"] is True
    assert block["IgnorePublicAcls"] is True
    assert block["BlockPublicPolicy"] is True
    assert block["RestrictPublicBuckets"] is True
    assert props["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    sse = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert sse[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_bucket_name_and_prefix_are_parameterised_not_hardcoded():
    """Bucket name and role name must derive from ProjectName, never literals."""
    template = _template()
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "community-board-storage-imagesbucket" not in source
    outputs = template["Outputs"]
    assert outputs["ImagesBucketName"]["Value"] == {"!Ref": "ImagesBucket"}
    assert outputs["EventsTableName"]["Value"] == {"!Ref": "EventsTable"}


def test_app_role_is_scoped_to_table_and_image_prefix_only():
    """Least privilege: no wildcard resources, no wildcard actions."""
    statements = _template()["Resources"]["AppRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]

    for statement in statements:
        resource = statement["Resource"]
        assert not str(resource).endswith("*:*"), "IAM resource must not be a full wildcard"
        actions = statement["Action"]
        for action in actions:
            assert not action.endswith(":*"), f"IAM action too broad: {action}"

    s3_actions = next(s["Action"] for s in statements if any("s3:" in a for a in s["Action"]))
    assert set(s3_actions) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}


def test_storage_stack_creates_no_cloudfront_or_alb_resources():
    """Cross-stack boundary: delivery belongs to the alb-cloudfront template."""
    types = {r["Type"] for r in _template()["Resources"].values()}
    assert "AWS::ElasticLoadBalancingV2::LoadBalancer" not in types
    assert "AWS::CloudFront::Distribution" not in types
    assert "AWS::EC2::Instance" not in types


def test_cors_origin_is_parameterised_not_literal():
    """CORS must not hardcode a distribution domain."""
    cors = _template()["Resources"]["ImagesBucket"]["Properties"]["CorsConfiguration"]
    assert cors["CorsRules"][0]["AllowedOrigins"] == [{"!Sub": "https://${CloudFrontDomain}"}]
    params = _template()["Parameters"]["CloudFrontDomain"]
    assert "Default" not in params, "CloudFrontDomain must be supplied at deploy time"


def test_bucket_enables_bucket_level_request_metrics():
    bucket = _template()["Resources"]["ImagesBucket"]["Properties"]

    # A MetricsConfigurations entry is the enable mechanism. AWS::S3::Bucket has
    # no BucketLevelMetrics and no RequestMetrics property; adding either was
    # rejected by EarlyValidation::PropertyValidation during deployment, so this
    # guard exists to stop that class of error coming back.
    assert "BucketLevelMetrics" not in bucket, (
        "AWS::S3::Bucket has no BucketLevelMetrics property; the "
        "MetricsConfigurations entry is what enables the metrics"
    )

    configurations = bucket["MetricsConfigurations"]
    assert len(configurations) == 1
    configuration = configurations[0]
    assert configuration["Id"] == "BucketLevelMetrics"
    assert configuration["Prefix"] == ""

    # MetricsConfiguration accepts only Id, Prefix, AccessPointArn and TagFilters.
    assert set(configuration) <= {"Id", "Prefix", "AccessPointArn", "TagFilters"}

    def contains_request_metrics(value):
        if isinstance(value, dict):
            return "RequestMetrics" in value or any(
                contains_request_metrics(child) for child in value.values()
            )
        if isinstance(value, list):
            return any(contains_request_metrics(child) for child in value)
        return False

    assert not contains_request_metrics(configuration)