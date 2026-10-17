"""Offline cross-reference checks for every CloudFormation template.

CloudFormation refuses a template containing a `!Ref` to something that is not
declared, and it does so part-way through a deploy. These checks catch the most
common form of that mistake -- a Ref to a parameter that was renamed, or a
resource logical id that was deleted -- without needing AWS credentials.

They also enforce the rules this project depends on: no hardcoded resource
identifiers or account ids, and stable stack names for idempotency.

Command: python -m pytest tests/test_templates_crossref.py -q
"""
from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"

TEMPLATES = [
    "storage.yaml",
    "compute.yaml",
    "alb-cloudfront.yaml",
    "monitoring.yaml",
    "monitoring-cloudfront.yaml",
]

# Identifiers recorded from a torn-down sandbox. None of these may reappear as
# literals in a template: every value must be a deploy-time parameter.
DEAD_IDENTIFIERS = [
    "vpc-0a59b0ab1751303bf",
    "subnet-0144d2792afff6ea3",
    "subnet-03622bef42a8797de",
    "sg-053179c7ab37b2959",
    "sg-0c3e6330600b8d362",
    "i-02e20dbd0f1788605",
    "community-board-storage-imagesbucket-wbuvewsvvaoa",
    "E1M0KDNO3ELOJH",
    "d14vm76rm7weav.cloudfront.net",
    "E38F33QVPMJ45",
]


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


def _load(name):
    with (INFRA / name).open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=CloudFormationLoader)


def _walk(node):
    """Yield every (tag, value) intrinsic function in a parsed template."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.startswith("!"):
                yield key, value
            else:
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_ref_resolves_to_a_declared_parameter_or_resource(name):
    """A Ref to an undeclared name is rejected by CloudFormation mid-deploy."""
    template = _load(name)
    declared = set(template.get("Parameters", {})) | set(template.get("Resources", {}))
    for logical_id, resource in template.get("Resources", {}).items():
        declared.add(logical_id)

    unresolved = []
    for tag, value in _walk(template):
        if tag == "!Ref":
            target = value if isinstance(value, str) else next(iter(value.values()))
            if isinstance(target, str) and not target.startswith("AWS::"):
                if target not in declared:
                    unresolved.append(f"!Ref {target}")

    assert not unresolved, f"{name} has undeclared references: {sorted(set(unresolved))}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_substitutions_only_use_declared_parameters(name):
    """!Sub must not interpolate a name the template does not declare."""
    import re

    template = _load(name)
    params = set(template.get("Parameters", {}))
    resources = set(template.get("Resources", {}))

    bad = set()
    for tag, value in _walk(template):
        if tag != "!Sub" or not isinstance(value, str):
            continue
        for token in re.findall(r"\$\{([^}]+)\}", value):
            if token.startswith("AWS::") or token in params or token in resources:
                continue
            # Resource attributes render as ${LogicalId.Attribute}
            if token.split(".")[0] in resources:
                continue
            bad.add(token)

    assert not bad, f"{name} substitutes undeclared names: {sorted(bad)}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_dead_resource_identifiers_are_hardcoded(name):
    """Recorded identifiers from a torn-down sandbox must never return as literals.

    Every value is a deploy-time parameter read from a stack output. Hardcoding
    one is how the previous sandbox ended up with an ALB pointed at a dead
    security group, serving 504 on every request.
    """
    source = (INFRA / name).read_text(encoding="utf-8")
    found = [i for i in DEAD_IDENTIFIERS if i in source]
    assert not found, f"{name} hardcodes identifiers from a dead sandbox: {found}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_account_id_or_credential_pattern(name):
    """Rule: never commit credentials or account ids."""
    import re

    source = (INFRA / name).read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", source), f"{name} appears to contain a 12-digit account id"
    assert "AKIA" not in source, f"{name} appears to contain an access key id"
    assert "aws_secret_access_key" not in source.lower(), f"{name} contains a secret key reference"


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_declares_required_metadata(name):
    template = _load(name)
    assert template.get("AWSTemplateFormatVersion") == "2010-09-09", f"{name} missing format version"
    assert template.get("Description"), f"{name} missing a description"
    assert template.get("Resources"), f"{name} declares no resources"


def test_stack_names_are_stable_for_idempotency():
    """The pipeline deploys by stack name; a rename breaks re-runs and cleanup."""
    pipeline = (ROOT / "ci" / "deploy.sh").read_text(encoding="utf-8")
    cleanup = (ROOT / "ci" / "cleanup.sh").read_text(encoding="utf-8")

    expected = {
        "community-board-storage",
        "community-board-compute",
        "community-board-alb-cloudfront",
        "community-board-monitoring",
        "community-board-monitoring-cloudfront",
    }
    for stack in expected:
        assert f"--stack-name {stack}" in pipeline, f"deploy.sh does not deploy {stack}"
        assert stack in cleanup, f"cleanup.sh does not delete {stack}"

    # Deletion order must be the reverse of creation order.
    order = [
        "community-board-monitoring-cloudfront",
        "community-board-monitoring",
        "community-board-alb-cloudfront",
        "community-board-compute",
        "community-board-storage",
    ]
    positions = [cleanup.index(f"  {s}\n") for s in order]
    assert positions == sorted(positions), "cleanup.sh does not delete in reverse dependency order"
    assert 'if [ "$1" = "community-board-monitoring-cloudfront" ]' in cleanup
    assert 'echo "us-east-1"' in cleanup
    assert 'delete-stack --region "$region" --stack-name "$stack"' in cleanup


def test_cloudfront_monitoring_uses_us_east_1_and_regional_monitoring_uses_pipeline_region():
    pipeline = (ROOT / "ci" / "deploy.sh").read_text(encoding="utf-8")
    cloudfront_pass = pipeline.split('echo "==> [6/6]', 1)[1]
    regional_pass = pipeline.split('echo "==> [5/6]', 1)[1].split('echo "==> [6/6]', 1)[0]
    assert "--region us-east-1" in cloudfront_pass
    assert '--region "$REGION"' in regional_pass
    assert "--region us-east-1" not in regional_pass


def test_target_group_lookup_uses_delivery_stack_output():
    pipeline = (ROOT / "ci" / "deploy.sh").read_text(encoding="utf-8")
    assert 'stack_output community-board-alb-cloudfront TargetGroupArn' in pipeline
    assert "TargetGroups[0]" not in pipeline
    assert 'ALB_DIMENSION="${ALB_ARN#*:loadbalancer/}"' in pipeline
    assert 'TG_DIMENSION="${TG_ARN##*:}"' in pipeline
    assert 'TG_DIMENSION="${TG_ARN#*:targetgroup/}"' not in pipeline


def test_deploy_labels_match_six_deployment_passes():
    pipeline = (ROOT / "ci" / "deploy.sh").read_text(encoding="utf-8")
    import re

    labels = re.findall(r"\[(\d+/\d+)\]", pipeline)
    assert labels == [f"{index}/6" for index in range(1, 7)]
    assert pipeline.count("aws cloudformation deploy \\") == 6


def test_direct_alb_probe_rejects_every_http_status_below_400():
    source = (ROOT / "ci" / "verify.sh").read_text(encoding="utf-8")
    import re

    match = re.search(r'\[\[ "\$direct_code" =~ (.+?) \]\]', source)
    assert match, "direct ALB status check must reject all 1xx-3xx responses"
    status_pattern = match.group(1)
    for status in ("100", "200", "302", "399"):
        assert re.fullmatch(status_pattern, status), f"status {status} must fail the negative control"
    assert not re.fullmatch(status_pattern, "400")
    assert '"$s3_code" = "403"' in source


def test_readme_discloses_deployed_application_stub():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "The deployed application is currently the Python stub embedded in `infra/compute.yaml`." in readme