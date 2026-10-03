"""Reglas de seguridad, resiliencia y entorno sobre las plantillas sintetizadas.

Cada regla es una función pura que recibe la plantilla de CloudFormation de una pila (el
JSON de ``cdk synth``) y devuelve la lista de :class:`Violation`, cada una con la pila, el
identificador lógico, el tipo y el nombre físico del recurso. Las pruebas de
``test_security_rules.py``, ``test_resiliency_rules.py`` y ``test_environments.py`` las
aplican a cada pila de cada despliegue y a recursos de prueba construidos a propósito.

Reglas (infrastructure-design §2.3 y sus notas de 2026-09-23):

- SECURITY-01: ningún almacenamiento sin cifrado con clave del cliente, salvo las tres
  excepciones declaradas por nombre (:data:`ENCRYPTION_EXCEPTIONS`).
- SECURITY-02: ningún balanceador sin registro de acceso.
- SECURITY-06: ninguna política con comodín no listado (:data:`RESOURCE_WILDCARDS`,
  :data:`NO_RESOURCE_LEVEL_ACTIONS`, :data:`ACTION_WILDCARDS`).
- SECURITY-07: ninguna entrada desde ``0.0.0.0/0`` o ``::/0`` salvo 443/TCP en el grupo de
  un balanceador.
- RESILIENCY-08, 09: en los despliegues permanentes, ``vigia-api`` ≥ 2 tareas y
  ``vigia-worker`` ≥ 1; 0 solo con ``first_deploy=true``; base con réplica en otra zona.
- NFR-NUC-31, RESILIENCY-12: ningún recurso fuera de ``us-east-1``; ninguna replicación.
- D-8: ``staging-<n>`` destruible entero y con nombres propios; ``pilot`` retenido.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

JsonObject = Mapping[str, Any]

REGION = "us-east-1"
# Región de AWS dentro de un texto: ``eu-west-1``, ``ap-southeast-2``, ``us-gov-west-1``,
# también seguida de la letra de una zona (``us-west-2a``).
_REGION_IN_TEXT = re.compile(
    r"\b[a-z]{2}(?:-gov|-iso[a-z]?)?-(?:north|south|east|west|central|"
    r"northeast|northwest|southeast|southwest)-[0-9](?![0-9])"
)
_ACCOUNT = r"(?:<AccountId>|[0-9]{12})"
_METADATA = "AWS::CDK::Metadata"


# --- Hallazgos ------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """Incumplimiento de una regla por un recurso concreto."""

    stack: str
    logical_id: str
    resource_type: str
    physical_name: str | None
    rule: str
    detail: str

    def __str__(self) -> str:
        name = f" {self.physical_name}" if self.physical_name else ""
        return (
            f"{self.stack}/{self.logical_id} [{self.resource_type}{name}]: "
            f"{self.detail} ({self.rule})"
        )


def describe(violations: Iterable[Violation]) -> str:
    """Una línea por hallazgo, para el mensaje de la aserción."""
    return "\n".join(str(v) for v in violations)


# --- Lectura de la plantilla ----------------------------------------------------------

# Propiedad que lleva el nombre físico, por tipo de recurso.
NAME_PROPERTIES: Mapping[str, str] = {
    "AWS::Backup::BackupVault": "BackupVaultName",
    "AWS::CloudWatch::Alarm": "AlarmName",
    "AWS::CloudWatch::Dashboard": "DashboardName",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::ECR::Repository": "RepositoryName",
    "AWS::ECS::Cluster": "ClusterName",
    "AWS::ECS::Service": "ServiceName",
    "AWS::EC2::SecurityGroup": "GroupName",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "Name",
    "AWS::ElasticLoadBalancingV2::TargetGroup": "Name",
    "AWS::ElasticLoadBalancingV2::TrustStore": "Name",
    "AWS::Events::Rule": "Name",
    "AWS::IAM::ManagedPolicy": "ManagedPolicyName",
    "AWS::IAM::Role": "RoleName",
    "AWS::KMS::Alias": "AliasName",
    "AWS::Logs::LogGroup": "LogGroupName",
    "AWS::RDS::DBCluster": "DBClusterIdentifier",
    "AWS::RDS::DBInstance": "DBInstanceIdentifier",
    "AWS::RDS::DBSubnetGroup": "DBSubnetGroupName",
    "AWS::Route53::HostedZone": "Name",
    "AWS::S3::Bucket": "BucketName",
    "AWS::SecretsManager::Secret": "Name",
    "AWS::SNS::Topic": "TopicName",
    "AWS::SQS::Queue": "QueueName",
    "AWS::SSM::Parameter": "Name",
    "AWS::WAFv2::WebACL": "Name",
}

# Nombres únicos en la cuenta o en la región: en ``staging-<n>`` deben llevar el sufijo del
# entorno para convivir con ``pilot`` (D-8). ``ServiceName`` y ``GroupName`` son únicos
# solo dentro de su clúster o su red, que ya llevan el sufijo.
ACCOUNT_UNIQUE_NAMES: frozenset[str] = frozenset(NAME_PROPERTIES) - {
    "AWS::ECS::Service",
    "AWS::EC2::SecurityGroup",
}


def resources(template: JsonObject) -> Iterator[tuple[str, JsonObject]]:
    """Recursos de la plantilla, sin los metadatos de CDK."""
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != _METADATA:
            yield logical_id, resource


def properties(resource: JsonObject) -> JsonObject:
    props: JsonObject = resource.get("Properties", {})
    return props


def _pseudo(name: str) -> str:
    """``AWS::AccountId`` → ``<AccountId>``: marcador sin ``:`` para no partir un ARN."""
    return f"<{name.removeprefix('AWS::')}>"


def render(value: Any, template: JsonObject) -> str:
    """Texto de un valor con funciones intrínsecas, para leer nombres y ARN.

    ``Ref`` a un pseudoparámetro queda como ``<AccountId>``; ``Fn::GetAtt`` del ``Arn`` de
    un depósito de la misma plantilla con nombre fijo se resuelve a su ARN; el resto de
    referencias quedan como ``<Ref.Id>`` o ``<GetAtt.Id.Atributo>``, sin comodines.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping) and len(value) == 1:
        (function, argument), *_ = value.items()
        if function == "Ref":
            return (
                _pseudo(str(argument)) if str(argument).startswith("AWS::") else f"<Ref.{argument}>"
            )
        if function == "Fn::GetAtt":
            logical_id, attribute = (
                argument if isinstance(argument, list) else str(argument).split(".", 1)
            )
            target = template.get("Resources", {}).get(logical_id, {})
            bucket_name = properties(target).get("BucketName")
            if target.get("Type") == "AWS::S3::Bucket" and attribute == "Arn" and bucket_name:
                return f"arn:<Partition>:s3:::{render(bucket_name, template)}"
            return f"<GetAtt.{logical_id}.{attribute}>"
        if function == "Fn::Join":
            separator, parts = argument
            return str(separator).join(render(part, template) for part in parts)
        if function == "Fn::Sub":
            text = argument if isinstance(argument, str) else argument[0]
            return re.sub(r"\$\{([^}]+)\}", lambda m: _pseudo(m.group(1)), str(text))
        return f"<{function}>"
    return f"<{type(value).__name__}>"


def physical_name(resource: JsonObject, template: JsonObject) -> str | None:
    """Nombre físico declarado del recurso, si lo tiene."""
    key = NAME_PROPERTIES.get(str(resource.get("Type")))
    value = properties(resource).get(key) if key else None
    return render(value, template) if value is not None else None


def reference_target(value: Any) -> str | None:
    """Identificador lógico al que apunta un ``Ref`` o un ``Fn::GetAtt``."""
    if isinstance(value, Mapping):
        if "Ref" in value:
            return str(value["Ref"])
        attribute = value.get("Fn::GetAtt")
        if isinstance(attribute, list):
            return str(attribute[0])
        if isinstance(attribute, str):
            return attribute.split(".", 1)[0]
    return None


class _Reporter:
    """Construye hallazgos con el contexto común de una pila y una regla."""

    def __init__(self, stack: str, template: JsonObject, rule: str) -> None:
        self.stack = stack
        self.template = template
        self.rule = rule
        self.found: list[Violation] = []

    def add(self, logical_id: str, resource: JsonObject, detail: str) -> None:
        self.found.append(
            Violation(
                stack=self.stack,
                logical_id=logical_id,
                resource_type=str(resource.get("Type")),
                physical_name=physical_name(resource, self.template),
                rule=self.rule,
                detail=detail,
            )
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


# --- SECURITY-01: cifrado con clave del cliente ---------------------------------------


@dataclass(frozen=True)
class EncryptionException:
    """Almacenamiento declarado por nombre que se cifra con la clave del servicio."""

    resource_type: str
    name: re.Pattern[str]
    accepted: Callable[[JsonObject], bool]
    reason: str


def _bucket_algorithms(props: JsonObject) -> list[JsonObject]:
    rules = props.get("BucketEncryption", {}).get("ServerSideEncryptionConfiguration", [])
    return [rule.get("ServerSideEncryptionByDefault", {}) for rule in rules]


def _bucket_sse_s3(props: JsonObject) -> bool:
    defaults = _bucket_algorithms(props)
    return bool(defaults) and all(d.get("SSEAlgorithm") == "AES256" for d in defaults)


def _registry_service_aes(props: JsonObject) -> bool:
    configuration = props.get("EncryptionConfiguration")
    return configuration is None or configuration.get("EncryptionType") == "AES256"


# Nota U02-H-06 de §2.3: las dos excepciones de U-02, listadas por nombre, y el depósito del
# conjunto sellado de U-01, que ``vigia-datasets`` trae de vigia-contracts con SSE-S3 (TASK-150).
ENCRYPTION_EXCEPTIONS: tuple[EncryptionException, ...] = (
    EncryptionException(
        resource_type="AWS::ECR::Repository",
        name=re.compile(r"^vigia-platform$"),
        accepted=_registry_service_aes,
        reason="registro de imágenes vigia-platform con AES-256 del servicio (§5.1)",
    ),
    EncryptionException(
        resource_type="AWS::S3::Bucket",
        # El heredado de U-01 y su copia efímera por entorno (nota de §2.1).
        name=re.compile(rf"^vigia-logs(-[a-z0-9-]+)?-{_ACCOUNT}-{REGION}$"),
        accepted=_bucket_sse_s3,
        reason="depósito heredado vigia-logs con SSE-S3 (§9.2, ciclo de vida de U-01)",
    ),
    EncryptionException(
        resource_type="AWS::S3::Bucket",
        # Único en la cuenta: solo pilot compartido, sin sufijo de despliegue (D-8).
        name=re.compile(rf"^vigia-datasets-{_ACCOUNT}-{REGION}$"),
        accepted=_bucket_sse_s3,
        reason="depósito del conjunto sellado de U-01 con SSE-S3 (contrato §4.2, TASK-150)",
    ),
)


def _bucket_customer_key(props: JsonObject) -> bool:
    defaults = _bucket_algorithms(props)
    return bool(defaults) and all(
        d.get("SSEAlgorithm") in {"aws:kms", "aws:kms:dsse"} and d.get("KMSMasterKeyID")
        for d in defaults
    )


def _has(key: str) -> Callable[[JsonObject], bool]:
    return lambda props: bool(props.get(key))


def _db_instance_encrypted(props: JsonObject) -> bool:
    # Una instancia de un clúster hereda el cifrado del clúster.
    if props.get("DBClusterIdentifier"):
        return True
    return props.get("StorageEncrypted") is True and bool(props.get("KmsKeyId"))


# Tipos de almacenamiento y cómo se reconoce su cifrado con clave del cliente.
CUSTOMER_KEY_CHECKS: Mapping[str, Callable[[JsonObject], bool]] = {
    "AWS::S3::Bucket": _bucket_customer_key,
    "AWS::RDS::DBInstance": _db_instance_encrypted,
    "AWS::RDS::DBCluster": lambda p: p.get("StorageEncrypted") is True and bool(p.get("KmsKeyId")),
    "AWS::ECR::Repository": lambda p: (
        p.get("EncryptionConfiguration", {}).get("EncryptionType") == "KMS"
        and bool(p.get("EncryptionConfiguration", {}).get("KmsKey"))
    ),
    "AWS::Logs::LogGroup": _has("KmsKeyId"),
    "AWS::SecretsManager::Secret": _has("KmsKeyId"),
    "AWS::Backup::BackupVault": _has("EncryptionKeyArn"),
    "AWS::DynamoDB::Table": lambda p: (
        p.get("SSESpecification", {}).get("SSEEnabled") is True
        and p.get("SSESpecification", {}).get("SSEType") == "KMS"
        and bool(p.get("SSESpecification", {}).get("KMSMasterKeyId"))
    ),
    "AWS::SQS::Queue": _has("KmsMasterKeyId"),
    "AWS::EFS::FileSystem": lambda p: p.get("Encrypted") is True and bool(p.get("KmsKeyId")),
    "AWS::EC2::Volume": lambda p: p.get("Encrypted") is True and bool(p.get("KmsKeyId")),
}


def check_storage_encryption(stack: str, template: JsonObject) -> list[Violation]:
    """SECURITY-01: todo almacenamiento cifrado con clave del cliente, salvo excepción."""
    report = _Reporter(stack, template, "SECURITY-01")
    for logical_id, resource in resources(template):
        kind = str(resource.get("Type"))
        check = CUSTOMER_KEY_CHECKS.get(kind)
        if check is None:
            continue
        props = properties(resource)
        if check(props):
            continue
        name = physical_name(resource, template) or ""
        if any(
            exception.resource_type == kind
            and exception.name.match(name)
            and exception.accepted(props)
            for exception in ENCRYPTION_EXCEPTIONS
        ):
            continue
        report.add(logical_id, resource, "almacenamiento sin cifrado con clave del cliente")
    return report.found


# --- SECURITY-02: registro de acceso de los balanceadores -----------------------------


def check_load_balancer_access_logs(stack: str, template: JsonObject) -> list[Violation]:
    """SECURITY-02: todo balanceador con registro de acceso a un depósito."""
    report = _Reporter(stack, template, "SECURITY-02")
    for logical_id, resource in resources(template):
        props = properties(resource)
        if resource.get("Type") == "AWS::ElasticLoadBalancingV2::LoadBalancer":
            attributes = {
                a.get("Key"): a.get("Value") for a in props.get("LoadBalancerAttributes", [])
            }
            enabled = attributes.get("access_logs.s3.enabled") == "true"
            if not (enabled and attributes.get("access_logs.s3.bucket")):
                report.add(logical_id, resource, "balanceador sin registro de acceso")
        elif resource.get("Type") == "AWS::ElasticLoadBalancing::LoadBalancer":
            policy = props.get("AccessLoggingPolicy", {})
            if not (policy.get("Enabled") is True and policy.get("S3BucketName")):
                report.add(logical_id, resource, "balanceador sin registro de acceso")
    return report.found


# --- SECURITY-06: comodines en políticas ----------------------------------------------


@dataclass(frozen=True)
class DeclaredWildcard:
    """Comodín de recurso documentado en §8 (o en la nota que se cita)."""

    name: str
    pattern: re.Pattern[str]
    source: str


# Acciones sin permisos a nivel de recurso: únicas admitidas con ``Resource: "*"`` (§8).
NO_RESOURCE_LEVEL_ACTIONS: frozenset[str] = frozenset(
    {
        "ecr:GetAuthorizationToken",
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "cloudwatch:PutMetricData",
        # Lectura de la canalización (``vigia-deploy``, TASK-151): comprobaciones nº 2 y 7 de
        # §3.2 y residuos de ``staging-<n>``.
        "cloudformation:ListStacks",
        "cloudwatch:DescribeAlarms",
        "cloudwatch:GetMetricStatistics",
        "elasticloadbalancing:DescribeTargetGroups",
        "tag:GetResources",
    }
)
# ``cloudwatch:PutMetricData`` solo con la condición de espacio de nombres (§8).
METRICS_NAMESPACE_CONDITION: JsonObject = {
    "StringEquals": {"cloudwatch:namespace": "Vigia/Platform"}
}

_S3 = r"^arn:[^:]+:s3:::"
RESOURCE_WILDCARDS: tuple[DeclaredWildcard, ...] = (
    DeclaredWildcard(
        "evidence-org",
        re.compile(rf"{_S3}vigia-evidence[^/*?]*/org/\*$"),
        "§8: GetObject y HeadObject sobre vigia-evidence/org/*",
    ),
    DeclaredWildcard(
        "evidence-upload-prefixes",
        re.compile(
            rf"{_S3}vigia-evidence[^/*?]*/org/\*/"
            r"(plant/\*/zone/\*/node/\*|plant/\*/documents/\*|closure/\*)$"
        ),
        "A-34: PutObject de vigia-api-task sobre node/, documents/ y closure/",
    ),
    DeclaredWildcard(
        "archive",
        re.compile(rf"{_S3}vigia-archive[^/*?]*/\*$"),
        "§8: PutObject y GetObject de vigia-worker-task sobre vigia-archive/*",
    ),
    DeclaredWildcard(
        "edge-ca",
        re.compile(rf"{_S3}vigia-edge[^/*?]*/ca/\*$"),
        "§8 y A-23: vigia-edge/ca/* (autoridad de nodos)",
    ),
    DeclaredWildcard(
        "drill",
        re.compile(rf"{_S3}vigia-drill[^/*?]*/\*$"),
        "deployment-architecture §6.1: depósito de ensayo vigia-drill",
    ),
    DeclaredWildcard(
        "ecs-cluster-tasks",
        re.compile(r"^arn:[^:]+:ecs:[^:]+:[^:]+:task/vigia-[a-z0-9-]+/\*$"),
        "§8: DescribeTasks de vigia-deploy sobre las tareas del clúster (vigia-migrate con "
        "--wait); el identificador de la tarea no se conoce antes de lanzarla",
    ),
    DeclaredWildcard(
        "backup-snapshots",
        re.compile(r"^arn:[^:]+:rds:[^:]+:[^:]+:snapshot:awsbackup:job-\*$"),
        "§8 y deployment-architecture §6.1: vigia-restore restaura desde las instantáneas "
        "mensuales de la bóveda, que AWS Backup nombra awsbackup:job-<id>",
    ),
    DeclaredWildcard(
        "access-log-delivery",
        re.compile(
            rf"{_S3}vigia-logs[^/*?]*/(alb/(app|nodes)|s3/(evidence|archive|edge|drill|datasets))"
            r"/[^*?]*\*$"
        ),
        "§4.2, §4.3 y §6.2: entrega de registros de acceso en sus prefijos de vigia-logs",
    ),
    DeclaredWildcard(
        "signing-secrets",
        re.compile(r"^arn:[^:]+:secretsmanager:[^:]+:[^:]+:secret:vigia/[a-z0-9-]+/signing/\*$"),
        "§8: vigia/<entorno>/signing/*",
    ),
    DeclaredWildcard(
        "secret-random-suffix",
        re.compile(r"^arn:[^:]+:secretsmanager:[^:]+:[^:]+:secret:vigia/[a-z0-9/_-]+-\?{6}$"),
        "§7.2: sufijo aleatorio de seis caracteres que Secrets Manager añade al nombre",
    ),
    DeclaredWildcard(
        "vigia-log-streams",
        re.compile(
            r"^arn:[^:]+:logs:[^:]+:[^:]+:log-group:/vigia/[a-z0-9-]+/"
            r"(\*|[a-z0-9/_-]+:\*|[a-z0-9/_-]+:log-stream:\*)$"
        ),
        "§8: CreateLogStream y PutLogEvents sobre /vigia/<entorno>/*",
    ),
    DeclaredWildcard(
        "pipeline-stacks",
        re.compile(r"^arn:[^:]+:cloudformation:[^:]+:[^:]+:stack/vigia-\*$"),
        "TASK-151: DescribeStacks de vigia-deploy sobre las pilas vigia-* (salidas para las "
        "tareas puntuales; staging-<n> lleva el número de ejecución en el nombre)",
    ),
    DeclaredWildcard(
        "staging-keys",
        re.compile(r"^arn:[^:]+:kms:[^:]+:[^:]+:key/\*$"),
        "TASK-151: DescribeKey de vigia-deploy, con la condición de etiqueta "
        "environment=staging-*, para comprobar el borrado programado de las claves de un staging",
    ),
)

# Comodines de acción declarados: solo el vaciado automático de los depósitos de
# ``staging`` (D-8), cuya sentencia genera CDK en la política del depósito para el rol de
# su proveedor de recurso personalizado. ``pilot`` no lo tiene (``check_permanent_retention``).
AUTO_DELETE_PROVIDER_ROLE = "CustomS3AutoDeleteObjectsCustomResourceProviderRole"
ACTION_WILDCARDS: frozenset[str] = frozenset({"s3:DeleteObject*", "s3:GetBucket*", "s3:List*"})
# Esa misma sentencia se aplica a todos los objetos del depósito que se vacía.
_AUTO_DELETE_RESOURCE = re.compile(r"^(arn:[^:]+:s3:::[^/*?]+|<GetAtt\.[A-Za-z0-9]+\.Arn>)/\*$")


def _is_wildcard(text: str) -> bool:
    return "*" in text or "?" in text


@dataclass(frozen=True)
class _Document:
    logical_id: str
    resource: JsonObject
    where: str
    document: JsonObject
    # ``identity``: política de identidad; ``resource``: política de recurso cuyo
    # ``Resource: "*"`` designa al propio recurso (clave, secreto, bóveda, registro);
    # ``explicit``: política de recurso con recursos explícitos (depósito, tema, cola,
    # registros); ``trust``: política de confianza de un rol.
    kind: str


def _documents(template: JsonObject) -> Iterator[_Document]:
    for logical_id, resource in resources(template):
        props = properties(resource)
        kind = resource.get("Type")
        if kind in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            yield _Document(
                logical_id, resource, "PolicyDocument", props["PolicyDocument"], "identity"
            )
        elif kind in {"AWS::IAM::Role", "AWS::IAM::User", "AWS::IAM::Group"}:
            for index, inline in enumerate(props.get("Policies", [])):
                yield _Document(
                    logical_id, resource, f"Policies[{index}]", inline["PolicyDocument"], "identity"
                )
            if "AssumeRolePolicyDocument" in props:
                yield _Document(
                    logical_id,
                    resource,
                    "AssumeRolePolicyDocument",
                    props["AssumeRolePolicyDocument"],
                    "trust",
                )
        elif kind == "AWS::S3::BucketPolicy":
            yield _Document(
                logical_id, resource, "PolicyDocument", props["PolicyDocument"], "explicit"
            )
        elif kind == "AWS::KMS::Key" and "KeyPolicy" in props:
            yield _Document(logical_id, resource, "KeyPolicy", props["KeyPolicy"], "resource")
        elif kind == "AWS::SecretsManager::ResourcePolicy":
            yield _Document(
                logical_id, resource, "ResourcePolicy", props["ResourcePolicy"], "resource"
            )
        elif kind == "AWS::Backup::BackupVault" and "AccessPolicy" in props:
            yield _Document(logical_id, resource, "AccessPolicy", props["AccessPolicy"], "resource")
        elif kind in {"AWS::SNS::TopicPolicy", "AWS::SQS::QueuePolicy"}:
            yield _Document(
                logical_id, resource, "PolicyDocument", props["PolicyDocument"], "explicit"
            )
        elif kind == "AWS::ECR::Repository" and "RepositoryPolicyText" in props:
            yield _Document(
                logical_id,
                resource,
                "RepositoryPolicyText",
                props["RepositoryPolicyText"],
                "resource",
            )
        elif kind == "AWS::Logs::ResourcePolicy":
            document = props["PolicyDocument"]
            parsed = json.loads(document) if isinstance(document, str) else document
            yield _Document(logical_id, resource, "PolicyDocument", parsed, "explicit")


def _principal_is_public(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, Mapping):
        return any("*" in _as_list(value) for value in principal.values())
    return False


def _is_auto_delete_statement(statement: JsonObject) -> bool:
    principal = statement.get("Principal", {})
    target = reference_target(principal.get("AWS")) if isinstance(principal, Mapping) else None
    return bool(target and target.startswith(AUTO_DELETE_PROVIDER_ROLE))


def check_policy_wildcards(stack: str, template: JsonObject) -> list[Violation]:
    """SECURITY-06: ninguna sentencia ``Allow`` con un comodín no listado."""
    report = _Reporter(stack, template, "SECURITY-06")
    for doc in _documents(template):
        for index, statement in enumerate(_as_list(doc.document.get("Statement"))):
            if statement.get("Effect") != "Allow":
                continue  # una denegación con comodín restringe, no concede
            where = f"{doc.where}.Statement[{index}]"
            if _principal_is_public(statement.get("Principal")):
                report.add(doc.logical_id, doc.resource, f"{where}: principal público '*'")
            for negated in ("NotAction", "NotResource", "NotPrincipal"):
                if negated in statement:
                    report.add(
                        doc.logical_id, doc.resource, f"{where}: {negated} en una sentencia Allow"
                    )
            if doc.kind == "trust":
                continue
            actions = [render(a, template) for a in _as_list(statement.get("Action"))]
            is_bucket_policy = doc.resource.get("Type") == "AWS::S3::BucketPolicy"
            auto_delete = is_bucket_policy and _is_auto_delete_statement(statement)
            for action in actions:
                if _is_wildcard(action) and not (auto_delete and action in ACTION_WILDCARDS):
                    report.add(
                        doc.logical_id, doc.resource, f"{where}: acción con comodín '{action}'"
                    )
            for raw in _as_list(statement.get("Resource")):
                text = render(raw, template)
                if text == "*":
                    if doc.kind == "resource":
                        continue  # en una política de recurso, "*" es el propio recurso
                    unlisted = sorted(set(actions) - NO_RESOURCE_LEVEL_ACTIONS)
                    if unlisted or not actions:
                        report.add(
                            doc.logical_id,
                            doc.resource,
                            f"{where}: recurso '*' para acciones no listadas {unlisted}",
                        )
                    elif "cloudwatch:PutMetricData" in actions and (
                        statement.get("Condition") != METRICS_NAMESPACE_CONDITION
                    ):
                        report.add(
                            doc.logical_id,
                            doc.resource,
                            f"{where}: cloudwatch:PutMetricData sin la condición de espacio de "
                            "nombres Vigia/Platform",
                        )
                elif auto_delete and _AUTO_DELETE_RESOURCE.match(text):
                    continue
                elif _is_wildcard(text) and not any(
                    w.pattern.match(text) for w in RESOURCE_WILDCARDS
                ):
                    report.add(
                        doc.logical_id,
                        doc.resource,
                        f"{where}: recurso con comodín no listado '{text}'",
                    )
    return report.found


# --- SECURITY-07: entrada desde Internet ----------------------------------------------

_PUBLIC_CIDRS = {"CidrIp": "0.0.0.0/0", "CidrIpv6": "::/0"}


def _load_balancer_groups(template: JsonObject) -> set[str]:
    groups: set[str] = set()
    for _, resource in resources(template):
        if resource.get("Type") in {
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "AWS::ElasticLoadBalancing::LoadBalancer",
        }:
            for group in properties(resource).get("SecurityGroups", []):
                target = reference_target(group)
                if target:
                    groups.add(target)
    return groups


def _public_rule_allowed(rule: JsonObject, group: str | None, lb_groups: set[str]) -> bool:
    return (
        group in lb_groups
        and str(rule.get("IpProtocol")) in {"tcp", "6"}
        and rule.get("FromPort") == 443
        and rule.get("ToPort") == 443
    )


def check_public_ingress(stack: str, template: JsonObject) -> list[Violation]:
    """SECURITY-07: entrada desde Internet solo por 443/TCP en el grupo de un balanceador."""
    report = _Reporter(stack, template, "SECURITY-07")
    lb_groups = _load_balancer_groups(template)
    for logical_id, resource in resources(template):
        kind = resource.get("Type")
        props = properties(resource)
        rules: list[tuple[JsonObject, str | None]]
        if kind == "AWS::EC2::SecurityGroup":
            rules = [(rule, logical_id) for rule in props.get("SecurityGroupIngress", [])]
        elif kind == "AWS::EC2::SecurityGroupIngress":
            rules = [(props, reference_target(props.get("GroupId")))]
        else:
            continue
        for rule, group in rules:
            source = next(
                (cidr for key, cidr in _PUBLIC_CIDRS.items() if rule.get(key) == cidr), None
            )
            if source and not _public_rule_allowed(rule, group, lb_groups):
                ports = f"{rule.get('IpProtocol')} {rule.get('FromPort')}-{rule.get('ToPort')}"
                report.add(
                    logical_id,
                    resource,
                    f"entrada desde {source} ({ports}) fuera de 443/TCP en un balanceador",
                )
    return report.found


SECURITY_RULES: tuple[Callable[[str, JsonObject], list[Violation]], ...] = (
    check_storage_encryption,
    check_load_balancer_access_logs,
    check_policy_wildcards,
    check_public_ingress,
)


# --- RESILIENCY-08, 09: tareas mínimas y réplica de la base ---------------------------

# Nota U02-H-06 de §2.3: en pilot, vigia-api ≥ 2 y vigia-worker ≥ 1.
MIN_TASKS: Mapping[str, int] = {"vigia-api": 2, "vigia-worker": 1}


def check_service_min_tasks(
    stack: str, template: JsonObject, *, first_deploy: bool
) -> list[Violation]:
    """Tareas mínimas; 0 solo con ``first_deploy=true`` (deployment-architecture §5, paso 4)."""
    report = _Reporter(stack, template, "RESILIENCY-08")
    scalable_targets = [
        (render(properties(r).get("ResourceId"), template), properties(r))
        for _, r in resources(template)
        if r.get("Type") == "AWS::ApplicationAutoScaling::ScalableTarget"
    ]
    for logical_id, resource in resources(template):
        if resource.get("Type") != "AWS::ECS::Service":
            continue
        name = physical_name(resource, template)
        minimum = MIN_TASKS.get(name or "")
        if minimum is None:
            continue
        counts = [("DesiredCount", properties(resource).get("DesiredCount"))]
        counts += [
            ("MinCapacity del escalado", props.get("MinCapacity"))
            for resource_id, props in scalable_targets
            if f"<GetAtt.{logical_id}." in resource_id or f"<Ref.{logical_id}>" in resource_id
        ]
        for label, count in counts:
            if not isinstance(count, int):
                report.add(logical_id, resource, f"{label} no declarado explícitamente")
            elif count == 0 and first_deploy:
                continue
            elif count < minimum:
                allowed = f"≥ {minimum}" + (" o 0 (first_deploy=true)" if first_deploy else "")
                report.add(logical_id, resource, f"{label} = {count}; se exige {allowed}")
    return report.found


def check_database_replica(stack: str, template: JsonObject) -> list[Violation]:
    """RESILIENCY-08: base con réplica en espera en otra zona (§6.1)."""
    report = _Reporter(stack, template, "RESILIENCY-08")
    members: dict[str, int] = {}
    for _, resource in resources(template):
        if resource.get("Type") == "AWS::RDS::DBInstance":
            cluster = reference_target(properties(resource).get("DBClusterIdentifier"))
            if cluster:
                members[cluster] = members.get(cluster, 0) + 1
    for logical_id, resource in resources(template):
        props = properties(resource)
        if resource.get("Type") == "AWS::RDS::DBInstance":
            if props.get("DBClusterIdentifier") or props.get("SourceDBInstanceIdentifier"):
                continue
            if props.get("MultiAZ") is not True:
                report.add(
                    logical_id, resource, "base sin réplica en espera en otra zona (MultiAZ)"
                )
        elif resource.get("Type") == "AWS::RDS::DBCluster" and members.get(logical_id, 0) < 2:
            report.add(logical_id, resource, "clúster de base con menos de dos instancias")
    return report.found


# --- NFR-NUC-31, RESILIENCY-12: región única y sin replicación ------------------------


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def check_region(stack: str, template: JsonObject, *, stack_region: str) -> list[Violation]:
    """NFR-NUC-31: la pila y todo lo que nombra una región, en ``us-east-1``."""
    report = _Reporter(stack, template, "NFR-NUC-31")
    if stack_region != REGION:
        report.found.append(
            Violation(
                stack,
                "(pila)",
                "AWS::CloudFormation::Stack",
                stack,
                report.rule,
                f"pila en la región {stack_region!r}; se exige {REGION}",
            )
        )
    for logical_id, resource in resources(template):
        regions = {
            match
            for text in _strings(resource.get("Properties", {}))
            for match in _REGION_IN_TEXT.findall(text)
            if match != REGION
        }
        for region in sorted(regions):
            report.add(logical_id, resource, f"referencia a la región {region} fuera de {REGION}")
        zone = properties(resource).get("AvailabilityZone")
        if isinstance(zone, str) and not zone.startswith(REGION):
            report.add(logical_id, resource, f"zona de disponibilidad {zone} fuera de {REGION}")
    return report.found


def check_no_replication(stack: str, template: JsonObject) -> list[Violation]:
    """RESILIENCY-12 y RNF-PRI-08: ninguna replicación hacia otra región (§11)."""
    report = _Reporter(stack, template, "RESILIENCY-12")
    for logical_id, resource in resources(template):
        kind = resource.get("Type")
        props = properties(resource)
        reason: str | None = None
        if kind == "AWS::S3::Bucket" and "ReplicationConfiguration" in props:
            reason = "depósito con replicación (§11: sin copia a otro depósito)"
        elif kind == "AWS::RDS::DBInstance" and (
            props.get("SourceRegion") or props.get("AutomaticBackupReplicationRegion")
        ):
            reason = "base con réplica o copias en otra región"
        elif kind == "AWS::RDS::DBInstance" and str(
            render(props.get("SourceDBInstanceIdentifier", ""), template)
        ).startswith("arn:"):
            reason = "réplica de lectura de una base de otra región"
        elif kind in {"AWS::RDS::GlobalCluster", "AWS::KMS::ReplicaKey"}:
            reason = "recurso global o réplica en otra región"
        elif kind == "AWS::KMS::Key" and props.get("MultiRegion") is True:
            reason = "clave KMS multirregión"
        elif kind == "AWS::SecretsManager::Secret" and props.get("ReplicaRegions"):
            reason = "secreto replicado en otra región"
        elif kind == "AWS::DynamoDB::GlobalTable" and any(
            replica.get("Region") != REGION for replica in props.get("Replicas", [])
        ):
            reason = "tabla global con réplica en otra región"
        elif kind == "AWS::ECR::ReplicationConfiguration":
            reason = "replicación del registro de imágenes"
        if reason:
            report.add(logical_id, resource, reason)
    return report.found


# --- D-8: staging efímero frente a pilot retenido -------------------------------------

_KEEP_POLICIES = {"Retain", "RetainExceptOnCreate", "Snapshot"}
# deployment-architecture §6.1: el depósito de ensayo es DESTROY también en pilot.
RETENTION_EXCEPTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^vigia-drill(-[a-z0-9-]+)?-{_ACCOUNT}-{REGION}$"),
)


def _auto_deleted_buckets(template: JsonObject) -> set[str]:
    return {
        target
        for _, resource in resources(template)
        if resource.get("Type") == "Custom::S3AutoDeleteObjects"
        and (target := reference_target(properties(resource).get("BucketName")))
    }


def check_ephemeral_teardown(
    stack: str, template: JsonObject, *, deployment: str
) -> list[Violation]:
    """D-8: todo ``staging-<n>`` se destruye entero y no choca con ``pilot``."""
    report = _Reporter(stack, template, "D-8")
    auto_deleted = _auto_deleted_buckets(template)
    for logical_id, resource in resources(template):
        kind = str(resource.get("Type"))
        props = properties(resource)
        for policy_key in ("DeletionPolicy", "UpdateReplacePolicy"):
            if resource.get(policy_key) in _KEEP_POLICIES:
                report.add(
                    logical_id,
                    resource,
                    f"{policy_key} {resource[policy_key]} en un entorno efímero",
                )
        if kind == "AWS::S3::Bucket":
            if props.get("ObjectLockEnabled") is True:
                report.add(logical_id, resource, "depósito con bloqueo de objetos en staging")
            if logical_id not in auto_deleted:
                report.add(logical_id, resource, "depósito sin vaciado automático en staging")
        if kind in {"AWS::RDS::DBInstance", "AWS::RDS::DBCluster"} and props.get(
            "DeletionProtection"
        ):
            report.add(logical_id, resource, "base con protección contra borrado en staging")
        if kind == "AWS::Route53::HostedZone":
            report.add(
                logical_id, resource, "zona DNS creada en staging (se importa por atributos)"
            )
        if kind == "AWS::WAFv2::WebACL":
            report.add(logical_id, resource, "cortafuegos de aplicación en staging")
        name = physical_name(resource, template)
        if kind in ACCOUNT_UNIQUE_NAMES and name is not None and deployment not in name:
            report.add(logical_id, resource, f"nombre físico sin el sufijo '{deployment}'")
    return report.found


def check_permanent_retention(stack: str, template: JsonObject) -> list[Violation]:
    """D-8 y P4: en un despliegue permanente, depósitos y base se retienen."""
    report = _Reporter(stack, template, "D-8")
    auto_deleted = _auto_deleted_buckets(template)
    for logical_id, resource in resources(template):
        kind = resource.get("Type")
        props = properties(resource)
        if kind == "AWS::S3::Bucket":
            name = physical_name(resource, template) or ""
            if any(pattern.match(name) for pattern in RETENTION_EXCEPTIONS):
                continue
            if resource.get("DeletionPolicy") != "Retain":
                report.add(logical_id, resource, "depósito sin DeletionPolicy Retain")
            if logical_id in auto_deleted:
                report.add(logical_id, resource, "depósito con vaciado automático")
        elif kind == "AWS::RDS::DBInstance" and not props.get("DBClusterIdentifier"):
            if props.get("DeletionProtection") is not True:
                report.add(logical_id, resource, "base sin protección contra borrado")
            if resource.get("DeletionPolicy") not in {"Retain", "Snapshot"}:
                report.add(logical_id, resource, "base sin retención ni instantánea final")
    return report.found
