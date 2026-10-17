import ast
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "compute.yaml"


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


def _resource(template, logical_id, resource_type=None):
    resource = template["Resources"][logical_id]
    if resource_type is not None:
        assert resource["Type"] == resource_type
    return resource["Properties"]


def test_vpc_has_two_public_subnets_and_exports_issue_5_handoff():
    template = _template()
    resources = template["Resources"]
    subnets = [
        value["Properties"]
        for value in resources.values()
        if value["Type"] == "AWS::EC2::Subnet"
    ]
    assert len(subnets) == 2
    assert all(subnet["Properties"]["MapPublicIpOnLaunch"] is False for subnet in resources.values() if subnet["Type"] == "AWS::EC2::Subnet")
    assert all(subnet["Properties"]["AvailabilityZone"]["!Select"][0] in (0, 1) for subnet in resources.values() if subnet["Type"] == "AWS::EC2::Subnet")
    assert "!Ref" in _resource(template, "PocVpc", "AWS::EC2::VPC")["CidrBlock"]
    assert "VpcId" in template["Outputs"]
    assert "PublicSubnetIds" in template["Outputs"]


def test_instance_uses_regional_al2023_ssm_parameter_and_t3_micro_only():
    template = _template()
    ami_parameter = template["Parameters"]["AmiId"]
    assert ami_parameter["Type"] == "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"
    assert ami_parameter["Default"].startswith("/aws/service/ami-amazon-linux-latest/al2023-")
    assert "ImageId" in _resource(template, "StubInstance", "AWS::EC2::Instance")
    assert template["Parameters"]["InstanceType"]["AllowedValues"] == ["t3.micro"]
    assert _resource(template, "StubInstance")["InstanceType"] == {"!Ref": "InstanceType"}


def test_role_is_required_and_attached_through_instance_profile():
    template = _template()
    role_parameter = template["Parameters"]["AppRoleArn"]
    assert role_parameter["Type"] == "String"
    assert "Default" not in role_parameter
    profile = _resource(template, "AppInstanceProfile", "AWS::IAM::InstanceProfile")
    assert profile["Roles"] == [{"!Select": [1, {"!Split": ["/", {"!Ref": "AppRoleArn"}]}]}]
    assert _resource(template, "StubInstance")["IamInstanceProfile"] == {"!Ref": "AppInstanceProfile"}
    assert "iam:PassRole" in role_parameter["Description"]


def test_security_groups_allow_only_alb_http_ingress_after_issue_5_cutover():
    template = _template()
    parameters = template["Parameters"]
    assert "TestClientCidr" not in parameters
    assert "0.0.0.0/0" not in str(parameters)

    ingress = _resource(template, "Ec2SecurityGroup", "AWS::EC2::SecurityGroup")["SecurityGroupIngress"]
    assert len(ingress) == 1
    assert all(rule["IpProtocol"] == "tcp" and rule["FromPort"] == 80 and rule["ToPort"] == 80 for rule in ingress)
    assert ingress[0]["SourceSecurityGroupId"] == {"!Ref": "AlbSecurityGroup"}
    assert "CidrIp" not in str(ingress)
    assert "SecurityGroupIngress" not in _resource(template, "AlbSecurityGroup", "AWS::EC2::SecurityGroup")
    assert not any(rule.get("FromPort") == 22 for rule in ingress)


def test_temporary_eip_is_released_and_handoff_outputs_exist():
    template = _template()
    assert "StubElasticIp" not in template["Resources"]
    assert "StubElasticIpAssociation" not in template["Resources"]
    assert "InstancePublicIp" not in template["Outputs"]
    for output in ("InstanceId", "AlbSecurityGroupId", "Ec2SecurityGroupId", "AppRoleArn"):
        assert output in template["Outputs"]


def test_user_data_avoids_network_dependent_package_install():
    """Instance boot must not depend on outbound network or the responder never starts.

    A `packages:` block makes cloud-init run package-update-upgrade-install first.
    Where egress is unavailable that module stalls inside modules:final, so
    scripts-user never runs and `systemctl enable --now` never executes.
    """
    template = _template()
    user_data = _resource(template, "StubInstance")["UserData"]["Fn::Base64"]["!Sub"]
    cloud_config = yaml.safe_load(user_data)
    assert "packages" not in cloud_config
    assert cloud_config["write_files"]
    assert cloud_config["runcmd"] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "community-board-stub.service"],
    ]


def test_user_data_responder_serves_health_events_and_not_found_locally():
    template = _template()
    user_data = _resource(template, "StubInstance")["UserData"]["Fn::Base64"]["!Sub"]
    cloud_config = yaml.safe_load(user_data)
    script = next(item["content"] for item in cloud_config["write_files"] if item["path"] == "/opt/community-board/server.py")
    ast.parse(script)

    namespace = {"__name__": "compute_template_test"}
    runnable_script = "\n".join(
        line for line in script.splitlines()
        if line.strip() != 'HTTPServer(("0.0.0.0", 80), Handler).serve_forever()'
    )
    exec(compile(runnable_script, "server.py", "exec"), namespace)
    server = HTTPServer(("127.0.0.1", 0), namespace["Handler"])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
            assert response.status == 200
            assert yaml.safe_load(response.read()) == {"status": "ok"}
        with urllib.request.urlopen(f"{base_url}/events", timeout=2) as response:
            assert response.status == 200
            assert yaml.safe_load(response.read()) == {"events": [], "stub": True}
        try:
            urllib.request.urlopen(f"{base_url}/not-found", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("Unknown paths must return 404")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_template_does_not_duplicate_neighbor_issue_resources_or_identity_strings():
    template = _template()
    resource_types = {resource["Type"] for resource in template["Resources"].values()}
    assert "AWS::DynamoDB::Table" not in resource_types
    assert "AWS::S3::Bucket" not in resource_types
    assert "AWS::ElasticLoadBalancingV2::LoadBalancer" not in resource_types
    assert "AWS::CloudFront::Distribution" not in resource_types
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "565393073702" not in source