"""infra/serverless.yml against the project's hard limits, and against the code it deploys.

The cost rules in CLAUDE.md (AWS Free plan) and the RDS shape are checked here, so an edit that
adds a NAT gateway or a bigger database fails before anyone deploys it. The other tests tie the
template to the code: every handler exists, the SSM names match scripts/aws_params.py, the CA
path matches the zip, and RDS runs the same Postgres as the local app-db.
"""

import importlib
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import aws_params, build_lambda
from scripts.envfile import REPO_ROOT

FORBIDDEN = {
    "AWS::EC2::NatGateway", "AWS::EC2::Instance", "AWS::EC2::EIP", "AWS::RDS::DBProxy",
    "AWS::RDS::DBCluster", "AWS::ElasticLoadBalancing::LoadBalancer",
    "AWS::ElasticLoadBalancingV2::LoadBalancer", "AWS::SecretsManager::Secret",
    "AWS::Lambda::Alias",
}  # fmt: skip


class CfnLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML is untyped
    """YAML with CloudFormation's short tags (!Ref, !GetAtt, !Join...) as plain mappings."""


def _cfn_tag(loader: CfnLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    text = (REPO_ROOT / "infra" / "serverless.yml").read_text()
    loaded: dict[str, Any] = yaml.load(text, Loader=CfnLoader)  # a SafeLoader subclass
    return loaded


def resources(config: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = config["resources"]["Resources"]
    return found


def test_nothing_the_free_plan_forbids(config: dict[str, Any]) -> None:
    types = {r["Type"] for r in resources(config).values()}
    assert not types & FORBIDDEN
    endpoints = [r for r in resources(config).values() if r["Type"] == "AWS::EC2::VPCEndpoint"]
    assert [e["Properties"]["VpcEndpointType"] for e in endpoints] == ["Gateway"]  # free
    for name, function in config["functions"].items():
        assert "provisionedConcurrency" not in function, name
        assert "reservedConcurrency" not in function, name


def test_the_database_is_the_agreed_shape(config: dict[str, Any]) -> None:
    db = resources(config)["Database"]["Properties"]
    assert (db["DBInstanceClass"], db["MultiAZ"], db["AllocatedStorage"], db["StorageType"]) == (
        "db.t4g.micro", False, "20", "gp3")  # fmt: skip
    assert (db["BackupRetentionPeriod"], db["EnablePerformanceInsights"]) == (1, False)
    assert "MaxAllocatedStorage" not in db  # no storage autoscaling
    assert "ManageMasterUserPassword" not in db  # that would create a Secrets Manager secret
    assert db["MasterUserPassword"].startswith("{{resolve:ssm-secure:")
    assert db["EnableIAMDatabaseAuthentication"] is True
    params = resources(config)["DatabaseParameters"]["Properties"]["Parameters"]
    assert params["rds.force_ssl"] == "1"
    assert params["rds.log_retention_period"] == str(7 * 24 * 60)


def test_rds_runs_the_local_app_db_postgres(config: dict[str, Any]) -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    local = set(re.findall(r"image: postgres:([0-9.]+)", compose))
    db = resources(config)["Database"]["Properties"]
    assert local == {db["EngineVersion"]}
    family = resources(config)["DatabaseParameters"]["Properties"]["Family"]
    assert family == f"postgres{db['EngineVersion'].split('.')[0]}"


def test_functions_are_python314_arm64_with_7_day_logs(config: dict[str, Any]) -> None:
    provider = config["provider"]
    assert (provider["runtime"], provider["architecture"]) == ("python3.14", "arm64")
    assert provider["logRetentionInDays"] == 7
    assert all("runtime" not in f for f in config["functions"].values())


@pytest.mark.parametrize("name", ["presign", "ingest", "notify", "status"])
def test_every_handler_exists(config: dict[str, Any], name: str) -> None:
    module, function = config["functions"][name]["handler"].rsplit(".", 1)
    assert callable(getattr(importlib.import_module(module.replace("/", ".")), function))


def test_the_ssm_names_are_the_ones_aws_params_writes(config: dict[str, Any]) -> None:
    assert config["custom"]["ssmPrefix"] == "${env:SSM_PREFIX}"  # what aws_params.py reads
    prefix = "${self:custom.ssmPrefix}"
    notify = config["functions"]["notify"]["environment"]
    assert notify["TUNNEL_URL_PARAM"] == f"{prefix}/{aws_params.TUNNEL}"
    assert notify["WEBHOOK_SECRET_PARAM"] == f"{prefix}/webhook-secret"
    password = resources(config)["Database"]["Properties"]["MasterUserPassword"]
    assert set(aws_params.SECURE) == {"webhook-secret", "rds-master-password"}
    assert f"{prefix}/rds-master-password" in password


def test_the_ca_path_is_where_the_zip_puts_the_bundle(config: dict[str, Any]) -> None:
    root_cert = config["custom"]["dbEnvironment"]["DB_SSLROOTCERT"]
    assert root_cert == "/var/task/certs/rds-global-bundle.pem"
    assert build_lambda.CA_BUNDLE.name == Path(root_cert).name


def test_uploads_and_rejects_expire_after_7_days(config: dict[str, Any]) -> None:
    rules = resources(config)["UploadBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    expiry = {r.get("Prefix"): r.get("ExpirationInDays") for r in rules}
    assert (expiry["uploads/"], expiry["rejects/"]) == (7, 7)


def test_lambda_zips_are_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("x = 1\n")
    first = build_lambda.write_zip(tmp_path / "1.zip", {"pkg/a.py": source})
    source.touch()  # a new mtime must not change the archive
    assert build_lambda.write_zip(tmp_path / "2.zip", {"pkg/a.py": source}) == first
    with zipfile.ZipFile(tmp_path / "1.zip") as archive:
        assert archive.namelist() == ["pkg/a.py"]


def test_security_group_descriptions_use_only_what_ec2_accepts(config: dict[str, Any]) -> None:
    # EC2 refuses any other character (an apostrophe failed the first deploy).
    allowed = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}$")
    texts = []
    for resource in resources(config).values():
        props = resource.get("Properties", {})
        texts += [props.get("GroupDescription", ""), props.get("Description", "")]
        for rule in props.get("SecurityGroupIngress", []) + props.get("SecurityGroupEgress", []):
            texts.append(rule.get("Description", ""))
    bad = [t for t in texts if isinstance(t, str) and not allowed.match(t)]
    assert bad == []


def test_the_notify_role_allows_the_ssm_call_the_handler_makes(config: dict[str, Any]) -> None:
    # moto doesn't enforce IAM, so the handler tests can't catch a missing permission.
    source = (REPO_ROOT / "services" / "notify" / "handler.py").read_text()
    statements = resources(config)["NotifyRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    actions = {a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str)
                                               else s["Action"])}  # fmt: skip
    assert ".get_parameters(" in source
    assert "ssm:GetParameters" in actions


def test_ingest_and_status_are_kept_logged_in(config: dict[str, Any]) -> None:
    """AD41: a 5-minute schedule pings both in-VPC functions, and each may be invoked by it.

    Their timeouts leave room for a cold IAM login; the handlers budget it from what's left.
    """
    rule = resources(config)["KeepWarm"]["Properties"]
    assert rule["ScheduleExpression"] == "rate(5 minutes)"
    targets = {t["Id"]: t for t in rule["Targets"]}
    assert set(targets) == {"ingest", "status"}
    assert all(t["Input"] == '{"warmup": true}' for t in targets.values())
    for name, function in (("IngestKeepWarmInvoke", "IngestLambdaFunction"),
                           ("StatusKeepWarmInvoke", "StatusLambdaFunction")):  # fmt: skip
        grant = resources(config)[name]["Properties"]
        assert grant["Principal"] == "events.amazonaws.com"
        assert grant["FunctionName"] == {"Fn::GetAtt": f"{function}.Arn"}
        assert grant["SourceArn"] == {"Fn::GetAtt": "KeepWarm.Arn"}
    functions = config["functions"]
    assert functions["ingest"]["timeout"] >= 120 + 60  # login budget cap + the file's reserve
    assert functions["status"]["timeout"] >= 50 + 5
