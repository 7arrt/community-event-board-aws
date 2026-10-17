"""Issue #5 template unit tests — ALB + CloudFront (test-after, minimal).

Covers FR1-FR8 / NFR1-NFR2 from
docs/inception.md
Command: python -m pytest tests/test_alb_cloudfront_template.py -q
"""
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "alb-cloudfront.yaml"
STORAGE_PATH = ROOT / "infra" / "storage.yaml"
COMPUTE_PATH = ROOT / "infra" / "compute.yaml"

CACHING_DISABLED_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"


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


def _load(path: Path):
    with path.open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=CloudFormationLoader)


def _template():
    return _load(TEMPLATE_PATH)


def test_alb_placement_listener_and_deletion_protection():
    """FR1 + FR8: internet-facing ALB, two public subnets, port 80, deletion protection Off."""
    template = _template()
    props = template["Resources"]["ApplicationLoadBalancer"]["Properties"]
    assert props["Scheme"] == "internet-facing"
    assert props["Type"] == "application"
    # Subnets must reference the two-AZ public subnet list parameter
    assert props["Subnets"] == {"!Ref": "PublicSubnetIds"}
    assert props["SecurityGroups"] == [{"!Ref": "AlbSecurityGroupId"}]
    attrs = {a["Key"]: a["Value"] for a in props["LoadBalancerAttributes"]}
    assert attrs.get("deletion_protection.enabled") == "false"

    listener = template["Resources"]["AlbListener"]["Properties"]
    assert listener["Port"] == 80
    assert listener["Protocol"] == "HTTP"
    assert listener["DefaultActions"][0]["Type"] == "forward"
    assert "TargetGroupArn" in listener["DefaultActions"][0]

    params = template["Parameters"]
    assert params["VpcId"]["Type"] == "AWS::EC2::VPC::Id"
    assert params["PublicSubnetIds"]["Type"] == "CommaDelimitedList"


def test_target_group_registers_instance_with_health_check():
    """FR2: target group registers EC2 stub, health check on /health port 80."""
    template = _template()
    props = template["Resources"]["AppTargetGroup"]["Properties"]
    assert props["Port"] == 80
    assert props["Protocol"] == "HTTP"
    assert props["TargetType"] == "instance"
    assert props["HealthCheckPath"] == "/health"
    assert props["Matcher"] == {"HttpCode": "200"}
    assert props["Targets"] == [{"Id": {"!Ref": "InstanceId"}}]
    assert template["Parameters"]["InstanceId"]["Type"] == "AWS::EC2::Instance::Id"


def test_origin_access_boundary_cloudfront_only():
    """FR3/NFR1: ALB ingress only from CloudFront prefix list; EC2 ingress only from ALB SG."""
    template = _template()
    ingress = template["Resources"]["AlbSgIngressCloudFront"]["Properties"]
    assert ingress["FromPort"] == 80 and ingress["ToPort"] == 80
    assert ingress["IpProtocol"] == "tcp"
    assert ingress["SourcePrefixListId"] == {"!Ref": "CfOriginPrefixListId"}

    egress = template["Resources"]["AlbSgEgressToEc2"]["Properties"]
    assert egress["DestinationSecurityGroupId"] == {"!Ref": "Ec2SecurityGroupId"}

    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "0.0.0.0/0" not in source
    assert "CidrIp" not in str(template["Resources"]["AlbSgIngressCloudFront"])


def test_distribution_origins_and_behaviors():
    """FR4: two origins (ALB + S3 REST with OAC), default->ALB, /images/*->S3, HTTPS redirect."""
    template = _template()
    dist = template["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
    assert dist["Enabled"] is True
    origins = {o["Id"]: o for o in dist["Origins"]}
    assert set(origins) == {"alb-origin", "s3-images-origin"}
    assert "CustomOriginConfig" in origins["alb-origin"]
    assert origins["alb-origin"]["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"
    assert "OriginAccessControlId" in origins["s3-images-origin"]

    assert dist["DefaultCacheBehavior"]["TargetOriginId"] == "alb-origin"
    assert dist["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    behaviors = dist["CacheBehaviors"]
    assert len(behaviors) == 1
    assert behaviors[0]["PathPattern"] == "/images/*"
    assert behaviors[0]["TargetOriginId"] == "s3-images-origin"
    assert behaviors[0]["ViewerProtocolPolicy"] == "redirect-to-https"


def test_both_cloudfront_behaviors_allow_required_http_methods():
    template = _template()
    dist = template["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
    required_methods = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
    behaviors = [dist["DefaultCacheBehavior"], *dist["CacheBehaviors"]]

    assert len(behaviors) == 2
    assert all(set(behavior["AllowedMethods"]) == required_methods for behavior in behaviors)
    assert all(set(behavior["AllowedMethods"]) != {"GET", "HEAD", "OPTIONS"} for behavior in behaviors)


def test_distribution_price_class_and_cache_policies():
    """FR5: PriceClass_100, CachingDisabled on default, CachingOptimized on /images/*."""
    template = _template()
    dist = template["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
    assert dist["PriceClass"] == "PriceClass_100"
    assert dist["DefaultCacheBehavior"]["CachePolicyId"] == CACHING_DISABLED_ID
    assert dist["CacheBehaviors"][0]["CachePolicyId"] == CACHING_OPTIMIZED_ID


def test_oac_bucket_policy_scoped_private():
    """FR6/NFR1: OAC bucket policy in new template, scoped to distribution, no public access."""
    template = _template()
    policy = template["Resources"]["ImagesBucketPolicy"]["Properties"]
    assert policy["Bucket"] == {"!Ref": "ImagesBucketName"}
    stmt = policy["PolicyDocument"]["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "s3:GetObject"
    # Prefix comes from the ImagePrefix parameter, not a duplicated literal.
    assert stmt["Resource"] == {"!Sub": "arn:aws:s3:::${ImagesBucketName}/${ImagePrefix}*"}
    assert template["Parameters"]["ImagePrefix"]["AllowedValues"] == ["images/"]
    assert stmt["Condition"]["StringEquals"]["AWS:SourceArn"]["!Sub"] == (
        "arn:aws:cloudfront::${AWS::AccountId}:distribution/${Distribution}"
    )
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "0.0.0.0" not in source
    # Bucket itself stays in storage stack, not duplicated here
    types = {r["Type"] for r in template["Resources"].values()}
    assert "AWS::S3::Bucket" not in types


def test_cors_lives_in_storage_stack_with_cloudfront_origin():
    """FR7: CorsConfiguration in infra/storage.yaml (not new template), CloudFront origin only."""
    assert "CorsConfiguration" not in TEMPLATE_PATH.read_text(encoding="utf-8")
    storage = _load(STORAGE_PATH)
    cors = storage["Resources"]["ImagesBucket"]["Properties"]["CorsConfiguration"]["CorsRules"][0]
    assert cors["AllowedOrigins"] == [{"!Sub": "https://${CloudFrontDomain}"}]
    methods = set(cors["AllowedMethods"])
    assert {"GET", "HEAD"} <= methods
    # The CloudFront domain is unknowable before deployment: it must be supplied,
    # never defaulted to some other distribution's domain.
    domain = storage["Parameters"]["CloudFrontDomain"]
    assert domain["Type"] == "String"
    assert "Default" not in domain
    assert "cloudfront.net" not in str(domain.get("Default", ""))
    # Storage resources + app-role permissions intact
    assert storage["Resources"]["EventsTable"]["Type"] == "AWS::DynamoDB::Table"
    actions = str(storage["Resources"]["AppRole"])
    assert "s3:GetObject" in actions and "s3:PutObject" in actions
