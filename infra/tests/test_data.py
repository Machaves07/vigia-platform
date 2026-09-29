"""Pila ``vigia-data`` (TASK-146): base, secretos de base, depósitos, bóveda y plan de copias.

Infrastructure-design §6 y §11 con sus notas de 2026-09-23 (tabla ``pilot`` frente a ``staging``
y ``vigia-logs-staging`` de §2.1, origen cruzado por entorno de §6.2), deployment-architecture
§6.1 (depósito de ensayo) y la adenda (A-22: prefijo ``documents/`` y subidas incompletas; A-33:
un solo origen por entorno). Cada prueba lee la plantilla sintetizada.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from functools import cache
from typing import Any

import pytest

from config import EnvironmentConfig
from stacks.data import DOMAIN_PARAMETER, BucketUsage, DbUser
from tests.conftest import Synthesized, synthesize
from tests.template_rules import (
    check_ephemeral_teardown,
    check_permanent_retention,
    check_policy_wildcards,
    check_storage_encryption,
    describe,
    properties,
    reference_target,
    render,
    resources,
)

JsonObject = Mapping[str, Any]
ACCOUNT = "<AccountId>"
LOGS_TARGET = re.compile(rf"^vigia-logs(-[a-z0-9-]+)?-{ACCOUNT}-us-east-1$")


def expected_data_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-data`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    buckets = 4 + (1 if config.access_logs_bucket_owned else 0)
    expected: Counter[str] = Counter(
        {
            "AWS::S3::Bucket": buckets,
            "AWS::S3::BucketPolicy": buckets,
            "AWS::RDS::DBSubnetGroup": 1,
            "AWS::RDS::DBParameterGroup": 1,
            "AWS::RDS::DBInstance": 1,
            "AWS::RDS::EventSubscription": 1,
            "AWS::Logs::LogGroup": 1,
            # Monitoreo mejorado y copias (vigia-backup).
            "AWS::IAM::Role": 2,
            "AWS::SecretsManager::Secret": 2,
            "AWS::SecretsManager::RotationSchedule": 2,
            "AWS::SecretsManager::ResourcePolicy": 2,
            "AWS::Backup::BackupVault": 1,
            "AWS::Backup::BackupPlan": 1,
            "AWS::Backup::BackupSelection": 1,
        }
    )
    if config.buckets_auto_delete_objects:
        # Proveedor del vaciado automático: su rol, su función y un recurso por depósito.
        expected += Counter(
            {
                "AWS::IAM::Role": 1,
                "AWS::Lambda::Function": 1,
                "Custom::S3AutoDeleteObjects": buckets,
            }
        )
    return expected


# --- Síntesis --------------------------------------------------------------------------


@cache
def _synth(**context: str) -> Synthesized:
    return synthesize(None, **context)


def _data(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("data")]


def _foundation(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("foundation")]


def _of_type(template: JsonObject, kind: str) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        if resource["Type"] == kind:
            yield logical_id, resource


def _one(template: JsonObject, kind: str) -> tuple[str, JsonObject]:
    (found,) = _of_type(template, kind)
    return found


def _tags(resource: JsonObject) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in properties(resource).get("Tags", [])}


def _buckets(template: JsonObject) -> dict[str, tuple[str, JsonObject]]:
    """Depósitos por nombre físico: ``vigia-<uso>[-<despliegue>]-<cuenta>-us-east-1``."""
    return {
        render(properties(bucket)["BucketName"], template): (logical_id, bucket)
        for logical_id, bucket in _of_type(template, "AWS::S3::Bucket")
    }


def _bucket(deployment: Synthesized, usage: str) -> tuple[str, JsonObject]:
    template = _data(deployment)
    return _buckets(template)[deployment.config.bucket_name(usage, ACCOUNT)]


def _bucket_statements(template: JsonObject, bucket_id: str) -> list[JsonObject]:
    for _, policy in _of_type(template, "AWS::S3::BucketPolicy"):
        if reference_target(properties(policy)["Bucket"]) == bucket_id:
            statements: list[JsonObject] = properties(policy)["PolicyDocument"]["Statement"]
            return statements
    raise AssertionError(f"{bucket_id} sin política")


def _foundation_key_alias(deployment: Synthesized, value: Any) -> str:
    """Alias de la clave de ``vigia-foundation`` a la que apunta ``Fn::GetStackOutput``."""
    output = value["Fn::GetStackOutput"]
    assert output["StackName"] == deployment.config.stack_name("foundation")
    foundation = _foundation(deployment)
    target = foundation["Outputs"][output["OutputName"]]["Value"]
    key_id = reference_target(target)
    assert target == {"Fn::GetAtt": [key_id, "Arn"]}
    assert foundation["Resources"][key_id]["Type"] == "AWS::KMS::Key"
    (alias,) = [
        str(properties(a)["AliasName"])
        for _, a in _of_type(foundation, "AWS::KMS::Alias")
        if reference_target(properties(a)["TargetKeyId"]) == key_id
    ]
    return alias


def _foundation_resource(deployment: Synthesized, value: Any) -> str:
    """Identificador lógico en ``vigia-foundation`` al que apunta ``Fn::GetStackOutput``."""
    output = value["Fn::GetStackOutput"]
    target = _foundation(deployment)["Outputs"][output["OutputName"]]["Value"]
    logical_id = reference_target(target)
    assert logical_id is not None
    return logical_id


def _key(deployment: Synthesized, name: str) -> str:
    return f"alias/{deployment.config.resource_name(name)}"


def _sse(bucket: JsonObject) -> JsonObject:
    (rule,) = properties(bucket)["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    return dict(rule)


# --- Base de datos (§6.1) ----------------------------------------------------------------


def _database(deployment: Synthesized) -> tuple[str, JsonObject]:
    return _one(_data(deployment), "AWS::RDS::DBInstance")


def test_pilot_database_is_multi_az_encrypted_with_vigia_db_forced_tls_and_protected(
    pilot: Synthesized,
) -> None:
    """Criterio 1: ``MultiAZ``, ``vigia-db``, ``rds.force_ssl=1`` y protección de borrado."""
    template = _data(pilot)
    _, database = _database(pilot)
    props = properties(database)
    assert props["DBInstanceIdentifier"] == "vigia-pilot-db"
    assert props["MultiAZ"] is True
    assert props["StorageEncrypted"] is True
    assert _foundation_key_alias(pilot, props["KmsKeyId"]) == "alias/vigia-db"
    assert props["DeletionProtection"] is True
    ((group_id, group),) = _of_type(template, "AWS::RDS::DBParameterGroup")
    assert reference_target(props["DBParameterGroupName"]) == group_id
    assert properties(group)["Parameters"]["rds.force_ssl"] == "1"
    # RETAIN con instantánea final si CloudFormation la sustituye.
    assert database["DeletionPolicy"] == "Retain"
    assert database["UpdateReplacePolicy"] == "Snapshot"


def test_pilot_database_follows_section_6_1(pilot: Synthesized) -> None:
    props = properties(_database(pilot)[1])
    assert props["Engine"] == "postgres"
    assert props["EngineVersion"] == "16"
    assert props["AutoMinorVersionUpgrade"] is True
    assert props["AllowMajorVersionUpgrade"] is False
    assert props["DBInstanceClass"] == "db.t4g.medium"
    assert props["StorageType"] == "gp3"
    assert props["AllocatedStorage"] == "100"
    assert props["MaxAllocatedStorage"] == 1000
    assert props["BackupRetentionPeriod"] == 35
    assert props["PreferredBackupWindow"] == "05:00-05:59"
    assert props["PreferredMaintenanceWindow"] == "sun:06:00-sun:07:00"
    assert props["MonitoringInterval"] == 60
    assert props["EnableCloudwatchLogsExports"] == ["postgresql"]
    assert props["CACertificateIdentifier"] == "rds-ca-rsa2048-g1"
    assert props["PubliclyAccessible"] is False
    assert props["CopyTagsToSnapshot"] is True
    assert props["DeleteAutomatedBackups"] is False
    assert _tags(_database(pilot)[1])["backup"] == "monthly"


def test_backup_and_maintenance_windows_do_not_overlap(pilot: Synthesized) -> None:
    props = properties(_database(pilot)[1])
    backup_start, backup_end = props["PreferredBackupWindow"].split("-")
    maintenance = props["PreferredMaintenanceWindow"]
    assert maintenance.startswith("sun:06:00")
    assert backup_start == "05:00" and backup_end < "06:00"


def test_parameter_group_vigia_pg16(deployment: Synthesized) -> None:
    ((_, group),) = _of_type(_data(deployment), "AWS::RDS::DBParameterGroup")
    props = properties(group)
    assert props["DBParameterGroupName"] == deployment.config.resource_name("pg16")
    assert props["Family"] == "postgres16"
    assert props["Parameters"] == {
        "rds.force_ssl": "1",
        "shared_preload_libraries": "pg_stat_statements",
        "log_min_duration_statement": "1000",
        "idle_in_transaction_session_timeout": "60000",
        "log_connections": "1",
    }


def test_database_lives_in_the_isolated_data_subnets_behind_sg_db(
    deployment: Synthesized,
) -> None:
    template = _data(deployment)
    props = properties(_database(deployment)[1])
    ((subnet_group_id, subnet_group),) = _of_type(template, "AWS::RDS::DBSubnetGroup")
    assert reference_target(props["DBSubnetGroupName"]) == subnet_group_id
    subnets = {_foundation_resource(deployment, s) for s in properties(subnet_group)["SubnetIds"]}
    foundation = _foundation(deployment)
    names = {_tags(foundation["Resources"][s])["Name"] for s in subnets}
    assert names == {deployment.config.resource_name(f"data-{z}") for z in "ab"}
    (group,) = props["VPCSecurityGroups"]
    group_id = _foundation_resource(deployment, group)
    assert properties(foundation["Resources"][group_id])["GroupName"] == "sg-db"


def test_master_password_is_managed_by_rds_with_vigia_secrets(deployment: Synthesized) -> None:
    """§6.1: ``vigia_owner`` con contraseña gestionada por RDS; ningún secreto en la plantilla."""
    props = properties(_database(deployment)[1])
    assert props["MasterUsername"] == "vigia_owner"
    assert props["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in props
    alias = _foundation_key_alias(deployment, props["MasterUserSecret"]["KmsKeyId"])
    assert alias == _key(deployment, "secrets")


def test_postgres_logs_go_to_an_encrypted_90_day_group_created_first(
    deployment: Synthesized,
) -> None:
    template = _data(deployment)
    database_id, database = _database(deployment)
    ((group_id, group),) = _of_type(template, "AWS::Logs::LogGroup")
    props = properties(group)
    identifier = f"vigia-{deployment.config.deployment}-db"
    assert props["LogGroupName"] == f"/aws/rds/instance/{identifier}/postgresql"
    assert props["RetentionInDays"] == 90
    assert _foundation_key_alias(deployment, props["KmsKeyId"]) == _key(deployment, "logs")
    assert group_id in database["DependsOn"]
    assert properties(database)["DBInstanceIdentifier"] == identifier
    assert database_id == "Database"


def test_enhanced_monitoring_role(deployment: Synthesized) -> None:
    template = _data(deployment)
    props = properties(_database(deployment)[1])
    role_id = reference_target(props["MonitoringRoleArn"])
    role = properties(template["Resources"][role_id])
    assert role["RoleName"] == deployment.config.resource_name("rds-monitoring")
    (statement,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"Service": "monitoring.rds.amazonaws.com"}
    (policy,) = role["ManagedPolicyArns"]
    assert render(policy, template).endswith(
        ":iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
    )


def test_database_events_go_to_vigia_alerts(deployment: Synthesized) -> None:
    template = _data(deployment)
    database_id, _ = _database(deployment)
    ((_, subscription),) = _of_type(template, "AWS::RDS::EventSubscription")
    props = properties(subscription)
    assert props["SourceType"] == "db-instance"
    assert props["SourceIds"] == [{"Ref": database_id}]
    assert props["EventCategories"] == ["failover", "failure", "maintenance"]
    assert props["Enabled"] is True
    topic_id = _foundation_resource(deployment, props["SnsTopicArn"])
    topic = _foundation(deployment)["Resources"][topic_id]
    assert properties(topic)["TopicName"] == deployment.config.resource_name("alerts")


def test_staging_database_is_small_unprotected_and_leaves_nothing(staging: Synthesized) -> None:
    """Tabla D-8: ``db.t4g.small``, sin protección, copias de 1 día, sin instantánea final."""
    _, database = _database(staging)
    props = properties(database)
    assert props["DBInstanceIdentifier"] == "vigia-staging-7-db"
    assert props["DBInstanceClass"] == "db.t4g.small"
    assert props["DeletionProtection"] is False
    assert props["BackupRetentionPeriod"] == 1
    assert props["DeleteAutomatedBackups"] is True
    assert database["DeletionPolicy"] == "Delete"
    assert database["UpdateReplacePolicy"] == "Delete"
    # Cifrado y TLS no cambian con el entorno.
    assert props["StorageEncrypted"] is True
    assert _foundation_key_alias(staging, props["KmsKeyId"]) == "alias/vigia-db-staging-7"


def test_dedicated_instance_database_is_like_pilot_with_its_own_name() -> None:
    deployment = _synth(instance="acme")
    _, database = _database(deployment)
    props = properties(database)
    assert props["DBInstanceIdentifier"] == "vigia-acme-db"
    assert props["MultiAZ"] is True and props["DeletionProtection"] is True
    assert database["DeletionPolicy"] == "Retain"


# --- Secretos de base (§7.2) ---------------------------------------------------------------


def _secrets(deployment: Synthesized) -> dict[str, tuple[str, JsonObject]]:
    return {
        str(properties(s)["Name"]): (logical_id, s)
        for logical_id, s in _of_type(_data(deployment), "AWS::SecretsManager::Secret")
    }


@pytest.mark.parametrize("user", list(DbUser), ids=lambda u: u.value)
def test_database_secrets_rotate_every_30_days_inside_the_vpc(
    deployment: Synthesized, user: DbUser
) -> None:
    template = _data(deployment)
    name = f"vigia/{deployment.config.deployment}/db/{user.value}"
    secret_id, secret = _secrets(deployment)[name]
    props = properties(secret)
    alias = _foundation_key_alias(deployment, props["KmsKeyId"])
    assert alias == _key(deployment, "secrets")
    generator = props["GenerateSecretString"]
    assert generator["GenerateStringKey"] == "password"
    assert generator["PasswordLength"] >= 32
    rendered = render(generator["SecretStringTemplate"], template)
    assert f'"username":"vigia_{user.value}"' in rendered
    assert '"engine":"postgres"' in rendered and '"dbname":"vigia"' in rendered
    (schedule,) = [
        properties(r)
        for _, r in _of_type(template, "AWS::SecretsManager::RotationSchedule")
        if reference_target(properties(r)["SecretId"]) == secret_id
    ]
    assert schedule["RotationRules"] == {"ScheduleExpression": "rate(30 days)"}
    # Los roles los crea la migración 0001 después del despliegue (§5.4).
    assert schedule["RotateImmediatelyOnUpdate"] is False
    hosted = schedule["HostedRotationLambda"]
    assert hosted["RotationType"] == "PostgreSQLSingleUser"
    assert hosted["RotationLambdaName"] == deployment.config.resource_name(
        f"db-{user.value}-rotation"
    )
    assert _foundation_key_alias(deployment, hosted["KmsKeyArn"]) == _key(deployment, "secrets")
    foundation = _foundation(deployment)
    group_id = _foundation_resource(deployment, hosted["VpcSecurityGroupIds"])
    assert properties(foundation["Resources"][group_id])["GroupName"] == "sg-tasks"
    subnet_parts = hosted["VpcSubnetIds"]["Fn::Join"][1][::2]
    subnets = {_foundation_resource(deployment, part) for part in subnet_parts}
    names = {_tags(foundation["Resources"][s])["Name"] for s in subnets}
    assert names == {deployment.config.resource_name(f"app-{z}") for z in "ab"}
    assert "Transform" in template and "AWS::SecretsManager-2024-09-16" in template["Transform"]


def test_no_plain_password_in_the_data_template(deployment: Synthesized) -> None:
    text = json.dumps(_data(deployment))
    assert '"MasterUserPassword"' not in text
    assert '"SecretString"' not in text


def test_secrets_are_retained_in_pilot_and_destroyed_in_staging(
    pilot: Synthesized, staging: Synthesized
) -> None:
    for _, secret in _secrets(pilot).values():
        assert secret["DeletionPolicy"] == "Retain"
    for _, secret in _secrets(staging).values():
        assert secret["DeletionPolicy"] == "Delete"
    assert set(_secrets(staging)) == {"vigia/staging-7/db/app", "vigia/staging-7/db/migrate"}


# --- Depósitos (§6.2) ----------------------------------------------------------------------


def test_every_bucket_blocks_public_access_and_requires_tls(deployment: Synthesized) -> None:
    """Criterio 3: ningún depósito tiene acceso público; todos solo por TLS."""
    template = _data(deployment)
    buckets = _buckets(template)
    assert buckets, "vigia-data sin depósitos"
    for name, (logical_id, bucket) in buckets.items():
        props = properties(bucket)
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, name
        assert props["OwnershipControls"] == {
            "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
        }, name
        statements = _bucket_statements(template, logical_id)
        tls = [
            s
            for s in statements
            if s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
        ]
        assert len(tls) == 1 and tls[0]["Effect"] == "Deny" and tls[0]["Action"] == "s3:*", name
        for statement in statements:
            if statement["Effect"] == "Allow":
                assert statement["Principal"] not in ("*", {"AWS": "*"}), name


def test_buckets_of_each_deployment(deployment: Synthesized) -> None:
    config = deployment.config
    names = set(_buckets(_data(deployment)))
    expected = {config.bucket_name(usage.value, ACCOUNT) for usage in BucketUsage}
    if config.access_logs_bucket_owned:
        expected.add(config.bucket_name("logs", ACCOUNT))
    assert names == expected


def test_pilot_bucket_names_are_those_of_the_design(pilot: Synthesized) -> None:
    assert set(_buckets(_data(pilot))) == {
        f"vigia-{usage}-{ACCOUNT}-us-east-1" for usage in ("evidence", "archive", "edge", "drill")
    }


def test_evidence_policy_rejects_plain_http_and_uploads_without_checksum(
    deployment: Synthesized,
) -> None:
    """Criterio 2 (primera mitad): sin TLS y sin ``x-amz-checksum-sha256`` no se sube nada."""
    template = _data(deployment)
    logical_id, _ = _bucket(deployment, "evidence")
    statements = _bucket_statements(template, logical_id)
    (checksum,) = [s for s in statements if s.get("Sid") == "DenyUploadsWithoutChecksum"]
    assert checksum["Effect"] == "Deny"
    assert checksum["Principal"] == {"AWS": "*"}
    assert checksum["Action"] == "s3:PutObject"
    assert render(checksum["Resource"], template) == (
        f"arn:<Partition>:s3:::{deployment.config.bucket_name('evidence', ACCOUNT)}/*"
    )
    assert checksum["Condition"] == {"Null": {"s3:x-amz-checksum-sha256": "true"}}
    (tls,) = [s for s in statements if "aws:SecureTransport" in json.dumps(s.get("Condition"))]
    assert tls["Effect"] == "Deny"
    rendered = sorted(render(r, template) for r in tls["Resource"])
    bucket_arn = f"arn:<Partition>:s3:::{deployment.config.bucket_name('evidence', ACCOUNT)}"
    assert rendered == [bucket_arn, f"{bucket_arn}/*"]
    # El proceso de la API solo firma URL: la política del depósito no concede escritura.
    for statement in statements:
        if statement["Effect"] == "Allow":
            assert "s3:PutObject" not in json.dumps(statement["Action"])


@pytest.mark.parametrize(
    ("context", "host"),
    [
        ({}, "app"),
        ({"first_deploy": "true"}, "app"),
        ({"instance": "acme"}, "app"),
        ({"environment": "staging-7"}, "staging-7"),
        ({"environment": "staging-1"}, "staging-1"),
        ({"environment": "staging-9999999"}, "staging-9999999"),
        ({"environment": "staging-7", "instance": "acme"}, "staging-7"),
    ],
    ids=[
        "pilot",
        "pilot-first-deploy",
        "dedicated",
        "staging-7",
        "staging-1",
        "staging-max",
        "dedicated-staging",
    ],
)
def test_evidence_has_one_cors_rule_with_the_single_origin_of_its_environment(
    context: dict[str, str], host: str
) -> None:
    """Criterio 2 (segunda mitad) y A-33: una sola regla, un solo origen, el del entorno."""
    deployment = _synth(**context)
    template = _data(deployment)
    _, bucket = _bucket(deployment, "evidence")
    (rule,) = properties(bucket)["CorsConfiguration"]["CorsRules"]
    (origin,) = rule["AllowedOrigins"]
    parts = origin["Fn::Join"][1]
    assert origin["Fn::Join"][0] == ""
    assert parts[0] == f"https://{host}."
    parameter = reference_target(parts[1])
    assert template["Parameters"][parameter] == {
        "Type": "AWS::SSM::Parameter::Value<String>",
        "Default": DOMAIN_PARAMETER,
    }
    assert len(parts) == 2
    assert rule["AllowedMethods"] == ["PUT", "GET", "HEAD"]
    assert rule["AllowedHeaders"] == ["Content-Type", "x-amz-checksum-sha256", "x-amz-meta-*"]
    assert rule["ExposedHeaders"] == ["ETag"]
    assert rule["MaxAge"] == 3600
    # Ningún otro depósito admite origen cruzado.
    for name, (_, other) in _buckets(template).items():
        if other is not bucket:
            assert "CorsConfiguration" not in properties(other), name
    # La política de pilot no lista orígenes de staging (A-33).
    if host == "app":
        assert "staging" not in json.dumps(rule)


def test_evidence_bucket_follows_section_6_2(pilot: Synthesized) -> None:
    _, bucket = _bucket(pilot, "evidence")
    props = properties(bucket)
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert props["ObjectLockEnabled"] is True
    assert props["ObjectLockConfiguration"] == {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 365}},
    }
    sse = _sse(bucket)
    assert sse["BucketKeyEnabled"] is True
    assert sse["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    key = sse["ServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    assert _foundation_key_alias(pilot, key) == "alias/vigia-evidence"
    rules = {rule["Id"]: rule for rule in props["LifecycleConfiguration"]["Rules"]}
    assert rules["InfrequentAccessAfter90Days"]["Transitions"] == [
        {"StorageClass": "STANDARD_IA", "TransitionInDays": 90}
    ]
    # Nº 22: subidas por partes incompletas abortadas a los 7 días.
    assert rules["AbortIncompleteMultipartUploads"]["AbortIncompleteMultipartUpload"] == {
        "DaysAfterInitiation": 7
    }
    # Sin expiración de ningún tipo: el registro no se borra (P4).
    for rule in rules.values():
        assert rule["Status"] == "Enabled"
        assert not {k for k in rule if "Expiration" in k}, rule["Id"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert _tags(bucket)["data"] == "evidence"


def test_archive_is_locked_in_compliance_mode_for_ten_years(
    permanent_deployment: Synthesized,
) -> None:
    """Criterio 3: ``vigia-archive`` con bloqueo en modo cumplimiento."""
    _, bucket = _bucket(permanent_deployment, "archive")
    props = properties(bucket)
    assert props["ObjectLockEnabled"] is True
    assert props["ObjectLockConfiguration"]["Rule"]["DefaultRetention"] == {
        "Mode": "COMPLIANCE",
        "Days": 3653,
    }
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    key = _sse(bucket)["ServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    assert _foundation_key_alias(permanent_deployment, key) == _key(permanent_deployment, "archive")
    (rule,) = props["LifecycleConfiguration"]["Rules"]
    assert rule["Transitions"] == [{"StorageClass": "DEEP_ARCHIVE", "TransitionInDays": 180}]
    assert not {k for k in rule if "Expiration" in k}
    assert bucket["DeletionPolicy"] == "Retain"
    assert _tags(bucket)["data"] == "audit-archive"


def test_drill_bucket_has_no_lock_uses_the_evidence_key_and_is_destroyable(
    deployment: Synthesized,
) -> None:
    """Criterio 3 y deployment-architecture §6.1: sin bloqueo, clave ``vigia-evidence``,
    ``DESTROY`` también en ``pilot``."""
    _, bucket = _bucket(deployment, "drill")
    props = properties(bucket)
    assert "ObjectLockEnabled" not in props
    assert "ObjectLockConfiguration" not in props
    key = _sse(bucket)["ServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    assert _foundation_key_alias(deployment, key) == _key(deployment, "evidence")
    assert bucket["DeletionPolicy"] == "Delete"
    assert props["LoggingConfiguration"]["LogFilePrefix"] == "s3/drill/"


def test_edge_bucket_holds_the_node_ca_with_vigia_secrets(deployment: Synthesized) -> None:
    _, bucket = _bucket(deployment, "edge")
    props = properties(bucket)
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert "ObjectLockEnabled" not in props
    key = _sse(bucket)["ServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    assert _foundation_key_alias(deployment, key) == _key(deployment, "secrets")
    assert _tags(bucket)["data"] == "public-keys"


def _logging_target(template: JsonObject, bucket: JsonObject) -> str:
    """Nombre del depósito de destino del registro de acceso."""
    destination = properties(bucket)["LoggingConfiguration"]["DestinationBucketName"]
    target = reference_target(destination)
    if target is None:
        return render(destination, template)
    return render(properties(template["Resources"][target])["BucketName"], template)


def test_every_bucket_logs_access_to_vigia_logs_under_its_prefix(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = _data(deployment)
    logs = config.bucket_name("logs", ACCOUNT)
    assert LOGS_TARGET.match(logs)
    for usage in BucketUsage:
        _, bucket = _bucket(deployment, usage.value)
        logging = properties(bucket)["LoggingConfiguration"]
        assert logging["LogFilePrefix"] == f"s3/{usage.value}/", usage
        assert _logging_target(template, bucket) == logs, usage


def test_pilot_logs_to_the_inherited_vigia_logs(pilot: Synthesized) -> None:
    """En ``pilot`` compartido, ``vigia-logs`` es de ``vigia-datasets``: no se crea aquí."""
    logs = f"vigia-logs-{ACCOUNT}-us-east-1"
    assert logs not in _buckets(_data(pilot))
    for usage in BucketUsage:
        _, bucket = _bucket(pilot, usage.value)
        destination = properties(bucket)["LoggingConfiguration"]["DestinationBucketName"]
        assert reference_target(destination) is None, usage  # por nombre, no por recurso
        assert render(destination, _data(pilot)) == logs, usage


def test_staging_7_has_no_locked_or_retained_bucket_and_logs_to_its_own_vigia_logs(
    staging: Synthesized,
) -> None:
    """Criterio 4, sobre toda la síntesis de ``staging-7``."""
    logs = f"vigia-logs-staging-7-{ACCOUNT}-us-east-1"
    all_buckets: dict[str, tuple[JsonObject, JsonObject]] = {}
    for stack_template in staging.templates.values():
        for name, (_, bucket) in _buckets(stack_template).items():
            all_buckets[name] = (stack_template, bucket)
    assert logs in all_buckets
    for name, (template, bucket) in all_buckets.items():
        props = properties(bucket)
        assert "ObjectLockEnabled" not in props, name
        assert "ObjectLockConfiguration" not in props, name
        assert bucket["DeletionPolicy"] == "Delete", name
        assert bucket["UpdateReplacePolicy"] == "Delete", name
        if name == logs:
            # El destino no se registra a sí mismo (lo desaconseja S3: bucle de registros).
            assert "LoggingConfiguration" not in props
            continue
        assert _logging_target(template, bucket) == logs, name
        assert name.startswith("vigia-") and "-staging-7-" in name, name


def test_staging_logs_bucket_is_the_declared_sse_s3_exception_and_accepts_delivery(
    staging: Synthesized,
) -> None:
    template = _data(staging)
    logs_id, logs = _buckets(template)[f"vigia-logs-staging-7-{ACCOUNT}-us-east-1"]
    props = properties(logs)
    defaults = _sse(logs)["ServerSideEncryptionByDefault"]
    assert defaults == {"SSEAlgorithm": "AES256"}
    assert props["LifecycleConfiguration"]["Rules"] == [
        {"ExpirationInDays": 365, "Id": "ExpireAfter365Days", "Status": "Enabled"}
    ]
    delivery = [
        s
        for s in _bucket_statements(template, logs_id)
        if s.get("Principal") == {"Service": "logging.s3.amazonaws.com"}
    ]
    # ``arn:...:s3:::vigia-logs-staging-7-.../s3/<uso>/*``
    prefixes = sorted(render(s["Resource"], template).rsplit("/", 3)[-2] for s in delivery)
    assert prefixes == sorted(u.value for u in BucketUsage)
    for statement in delivery:
        assert statement["Action"] == "s3:PutObject"
        assert statement["Condition"]["StringEquals"] == {
            "aws:SourceAccount": {"Ref": "AWS::AccountId"}
        }


def test_staging_buckets_are_emptied_automatically(staging: Synthesized) -> None:
    template = _data(staging)
    emptied = {
        reference_target(properties(r)["BucketName"])
        for _, r in _of_type(template, "Custom::S3AutoDeleteObjects")
    }
    assert emptied == {logical_id for logical_id, _ in _buckets(template).values()}


def test_permanent_buckets_are_not_emptied_automatically(
    permanent_deployment: Synthesized,
) -> None:
    template = _data(permanent_deployment)
    assert list(_of_type(template, "Custom::S3AutoDeleteObjects")) == []
    assert list(_of_type(template, "AWS::Lambda::Function")) == []


def test_dedicated_instance_creates_its_own_retained_vigia_logs() -> None:
    deployment = _synth(instance="acme")
    _, logs = _buckets(_data(deployment))[f"vigia-logs-acme-{ACCOUNT}-us-east-1"]
    assert logs["DeletionPolicy"] == "Retain"


# --- Bóveda y plan de copias (§11) ---------------------------------------------------------


def test_monthly_snapshots_of_this_database_go_to_the_backup_vault(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = _data(deployment)
    vault_id, vault = _one(template, "AWS::Backup::BackupVault")
    props = properties(vault)
    assert props["BackupVaultName"] == config.resource_name("backup-vault")
    assert _foundation_key_alias(deployment, props["EncryptionKeyArn"]) == _key(
        deployment, "backup"
    )
    plan_id, plan = _one(template, "AWS::Backup::BackupPlan")
    body = properties(plan)["BackupPlan"]
    assert body["BackupPlanName"] == config.resource_name("monthly-snapshots")
    (rule,) = body["BackupPlanRule"]
    assert rule["ScheduleExpression"] == "cron(0 2 1 * ? *)"  # día 1, 02:00 UTC
    assert rule["Lifecycle"] == {"DeleteAfterDays": 365}
    assert rule["TargetBackupVault"] == {"Fn::GetAtt": [vault_id, "BackupVaultName"]}
    _, selection = _one(template, "AWS::Backup::BackupSelection")
    chosen = properties(selection)["BackupSelection"]
    assert properties(selection)["BackupPlanId"] == {"Fn::GetAtt": [plan_id, "BackupPlanId"]}
    assert chosen["Conditions"] == {
        "StringEquals": [{"ConditionKey": "aws:ResourceTag/backup", "ConditionValue": "monthly"}]
    }
    (resource,) = chosen["Resources"]
    assert render(resource, template) == (
        f"arn:aws:rds:us-east-1:{ACCOUNT}:db:vigia-{config.deployment}-db"
    )
    assert "ListOfTags" not in chosen  # una selección solo por etiqueta tomaría otras bases
    role = properties(template["Resources"][reference_target(chosen["IamRoleArn"])])
    assert role["RoleName"] == config.resource_name("backup")
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "backup.amazonaws.com"}
    (policy,) = role["ManagedPolicyArns"]
    assert render(policy, template).endswith(
        ":iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
    )


def test_backup_role_is_the_one_named_by_the_key_policies(deployment: Synthesized) -> None:
    """``vigia-db`` y ``vigia-backup`` conceden uso al rol ``vigia-backup`` por su ARN (§7.1)."""
    role_name = deployment.config.resource_name("backup")
    assert role_name in json.dumps(_foundation(deployment))
    roles = {
        properties(r).get("RoleName") for _, r in _of_type(_data(deployment), "AWS::IAM::Role")
    }
    assert role_name in roles


def test_vault_is_retained_in_pilot_and_destroyed_in_staging(
    pilot: Synthesized, staging: Synthesized
) -> None:
    assert _one(_data(pilot), "AWS::Backup::BackupVault")[1]["DeletionPolicy"] == "Retain"
    assert _one(_data(staging), "AWS::Backup::BackupVault")[1]["DeletionPolicy"] == "Delete"


# --- Reglas de plantilla sobre vigia-data ----------------------------------------------------


def test_data_stack_passes_the_template_rules(deployment: Synthesized) -> None:
    name = deployment.config.stack_name("data")
    template = _data(deployment)
    violations = check_storage_encryption(name, template) + check_policy_wildcards(name, template)
    if deployment.config.ephemeral:
        violations += check_ephemeral_teardown(
            name, template, deployment=deployment.config.deployment
        )
    else:
        violations += check_permanent_retention(name, template)
    assert violations == [], describe(violations)


def test_data_stack_does_not_touch_the_foundation_key_policies(
    pilot: Synthesized,
) -> None:
    """Las claves se importan por ARN: ningún constructo de ``vigia-data`` añade sentencias sin
    ``Sid`` (como las de CDK) a las políticas de ``vigia-foundation``."""
    for _, key in _of_type(_foundation(pilot), "AWS::KMS::Key"):
        for statement in properties(key)["KeyPolicy"]["Statement"]:
            assert statement.get("Sid"), statement
