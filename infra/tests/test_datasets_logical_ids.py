"""Traslado de ``vigia-datasets`` sin recrear sus depósitos (TASK-150, deployment-architecture §8).

La referencia es la plantilla que sintetiza ``vigia-contracts/infra`` en el commit
``467ebf3f425a4177996751ddc447d15965b701f1`` (``tests/fixtures/``, sin ``CDKMetadata``). Para
regenerarla tras un cambio en ``vigia-contracts``: clonar el repositorio y, en su ``infra/``,
``uv sync --frozen && npx aws-cdk synth vigia-datasets --json``, quitar el recurso
``CDKMetadata`` (cambia con la versión de CDK) y guardar con claves ordenadas. Procedimiento del
traslado en ``docs/runbooks/traslado-vigia-datasets.md``.

- Ningún recurso de la referencia cambia de identificador lógico ni de tipo; los depósitos
  conservan todas sus propiedades salvo las etiquetas, que CloudFormation actualiza sin
  reemplazo. Los dos presupuestos no se trasladan: ``vigia-foundation`` los declara con el
  mismo nombre (un nombre de presupuesto es único en la cuenta).
- La política de ``vigia-logs`` conserva sus sentencias y concede la entrega de los registros
  de acceso de ``vigia-data`` y de los balanceadores de ``vigia-edge`` de ``pilot`` compartido.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import DEPLOYMENTS, Synthesized, synthesize
from tests.template_rules import properties, render, resources

REFERENCE = Path(__file__).parent / "fixtures" / "vigia-contracts-datasets.template.json"
STACK = "vigia-datasets"
# Recursos de la referencia que ya declara ``vigia-foundation`` (TASK-145).
MOVED_TO_FOUNDATION = {"MonthlyBudget": "vigia-monthly", "StagingBudget": "vigia-staging"}
RETAINED_BUCKETS = ("DatasetsBucket", "LogsBucket")
ELB_ACCOUNT_ROOT = "arn:aws:iam::127311923021:root"
_S3_ARN = re.compile(r"^arn:[^:]+:s3:::")


def _reference() -> dict[str, Any]:
    template: dict[str, Any] = json.loads(REFERENCE.read_text(encoding="utf-8"))
    return template


def _resource(template: Mapping[str, Any], logical_id: str) -> Mapping[str, Any]:
    resource: Mapping[str, Any] = template["Resources"][logical_id]
    return resource


def _without_tags(resource: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **resource,
        "Properties": {k: v for k, v in properties(resource).items() if k != "Tags"},
    }


def _tags(resource: Mapping[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in properties(resource).get("Tags", [])}


def _statements(template: Mapping[str, Any], logical_id: str) -> list[Mapping[str, Any]]:
    statements: list[Mapping[str, Any]] = properties(_resource(template, logical_id))[
        "PolicyDocument"
    ]["Statement"]
    return statements


def _texts(value: Any, template: Mapping[str, Any]) -> list[str]:
    """Una cadena o una lista de valores, ya renderizados y con la partición literal."""
    values = value if isinstance(value, list) else [value]
    return [_S3_ARN.sub("arn:aws:s3:::", render(v, template)) for v in values]


def _grants(
    template: Mapping[str, Any], principal: Mapping[str, str]
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """``(recurso, condiciones)`` de cada ``s3:PutObject`` permitido a ``principal``."""
    for statement in _statements(template, "LogsBucketPolicy"):
        if statement["Effect"] != "Allow" or statement["Action"] != "s3:PutObject":
            continue
        rendered = {k: _texts(v, template)[0] for k, v in statement["Principal"].items()}
        if rendered != principal:
            continue
        for resource in _texts(statement["Resource"], template):
            yield resource, statement.get("Condition", {})


@pytest.fixture(scope="module")
def datasets(pilot: Synthesized) -> Mapping[str, Any]:
    return pilot.templates[STACK]


# --- Identificadores lógicos --------------------------------------------------------------


def test_reference_is_the_vigia_contracts_synthesis() -> None:
    reference = _reference()
    assert {k: r["Type"] for k, r in resources(reference)} == {
        "DatasetsBucket": "AWS::S3::Bucket",
        "DatasetsBucketPolicy": "AWS::S3::BucketPolicy",
        "LogsBucket": "AWS::S3::Bucket",
        "LogsBucketPolicy": "AWS::S3::BucketPolicy",
        "MonthlyBudget": "AWS::Budgets::Budget",
        "StagingBudget": "AWS::Budgets::Budget",
    }


def test_no_resource_changes_its_logical_id(datasets: Mapping[str, Any]) -> None:
    """Cada recurso de la referencia sigue en ``vigia-datasets`` con el mismo identificador y
    tipo, salvo los presupuestos, que no se trasladan; y no aparece ningún recurso nuevo."""
    reference = {k: r["Type"] for k, r in resources(_reference())}
    found = {k: r["Type"] for k, r in resources(datasets)}
    expected = {k: t for k, t in reference.items() if k not in MOVED_TO_FOUNDATION}
    assert found == expected


def test_budgets_not_moved_are_declared_by_foundation(pilot: Synthesized) -> None:
    foundation = pilot.templates["vigia-foundation"]
    reference = _reference()
    for logical_id, name in MOVED_TO_FOUNDATION.items():
        budget = _resource(foundation, logical_id)
        assert budget["Type"] == _resource(reference, logical_id)["Type"]
        assert properties(budget)["Budget"]["BudgetName"] == name
        assert properties(_resource(reference, logical_id))["Budget"]["BudgetName"] == name


def test_only_the_budget_parameter_goes_away(datasets: Mapping[str, Any]) -> None:
    """``BudgetAlertEmail`` solo lo usaban los presupuestos; ``vigia-foundation`` avisa por
    ``vigia-alerts``."""
    assert set(_reference()["Parameters"]) - set(datasets["Parameters"]) == {"BudgetAlertEmail"}
    assert set(datasets["Parameters"]) == {"BootstrapVersion"}


@pytest.mark.parametrize("logical_id", RETAINED_BUCKETS)
def test_buckets_keep_every_property_but_their_tags(
    datasets: Mapping[str, Any], logical_id: str
) -> None:
    """Nombre, bloqueo de objetos, cifrado, ciclo de vida, registro de acceso y políticas de
    retención idénticos: nada que obligue a CloudFormation a reemplazar el depósito."""
    reference = _resource(_reference(), logical_id)
    found = _resource(datasets, logical_id)
    assert _without_tags(found) == _without_tags(reference)
    assert found["DeletionPolicy"] == found["UpdateReplacePolicy"] == "Retain"


@pytest.mark.parametrize("logical_id", RETAINED_BUCKETS)
def test_bucket_tags_keep_the_data_class(datasets: Mapping[str, Any], logical_id: str) -> None:
    assert _tags(_resource(_reference(), logical_id))["data"] == "anonymized"
    assert _tags(_resource(datasets, logical_id)) == {
        "data": "anonymized",
        "environment": "pilot",
        "managed_by": "cdk",
        "project": "vigia",
        "unit": "U-02",
    }


def test_datasets_policy_is_unchanged(datasets: Mapping[str, Any]) -> None:
    assert _resource(datasets, "DatasetsBucketPolicy") == _resource(
        _reference(), "DatasetsBucketPolicy"
    )


def test_logs_policy_keeps_the_reference_statements(datasets: Mapping[str, Any]) -> None:
    reference = _resource(_reference(), "LogsBucketPolicy")
    found = _resource(datasets, "LogsBucketPolicy")
    assert properties(found)["Bucket"] == properties(reference)["Bucket"]
    statements = _statements(datasets, "LogsBucketPolicy")
    for statement in _statements(_reference(), "LogsBucketPolicy"):
        assert statement in statements


# --- Entrega de registros de acceso en el vigia-logs heredado (VIG-34, VIG-41) -----------


def _logged_buckets(template: Mapping[str, Any]) -> Iterator[tuple[str, str, str]]:
    """``(depósito, destino, prefijo)`` de cada depósito con registro de acceso."""
    for _, resource in resources(template):
        if resource["Type"] != "AWS::S3::Bucket":
            continue
        logging = properties(resource).get("LoggingConfiguration")
        if logging:
            yield (
                render(properties(resource)["BucketName"], template),
                render(logging["DestinationBucketName"], template),
                str(logging["LogFilePrefix"]),
            )


def test_pilot_data_buckets_log_to_the_inherited_logs_bucket(pilot: Synthesized) -> None:
    logged = list(_logged_buckets(pilot.templates["vigia-data"]))
    assert {prefix for _, _, prefix in logged} == {
        "s3/evidence/",
        "s3/archive/",
        "s3/edge/",
        "s3/drill/",
    }
    assert {target for _, target, _ in logged} == {"vigia-logs-<AccountId>-us-east-1"}


def test_logs_policy_accepts_every_bucket_that_logs_to_it(
    pilot: Synthesized, datasets: Mapping[str, Any]
) -> None:
    """Cada depósito de ``vigia-data`` y el del conjunto escriben solo en su prefijo, limitados
    a su propio depósito y a la cuenta."""
    grants = list(_grants(datasets, {"Service": "logging.s3.amazonaws.com"}))
    sources = [*_logged_buckets(pilot.templates["vigia-data"]), *_logged_buckets(datasets)]
    assert len(grants) == len(sources) == 5
    for bucket, _, prefix in sources:
        resource = f"arn:aws:s3:::vigia-logs-<AccountId>-us-east-1/{prefix}*"
        matching = [conditions for found, conditions in grants if found == resource]
        assert len(matching) == 1, prefix
        conditions = matching[0]
        assert _texts(conditions["ArnLike"]["aws:SourceArn"], datasets) == [
            f"arn:aws:s3:::{bucket}"
        ], prefix
        assert _texts(conditions["StringEquals"]["aws:SourceAccount"], datasets) == ["<AccountId>"]


def _load_balancer_prefixes(template: Mapping[str, Any]) -> set[str]:
    prefixes: set[str] = set()
    for _, resource in resources(template):
        if resource["Type"] != "AWS::ElasticLoadBalancingV2::LoadBalancer":
            continue
        attributes = {
            a["Key"]: a["Value"] for a in properties(resource).get("LoadBalancerAttributes", [])
        }
        assert render(attributes["access_logs.s3.bucket"], template) == (
            "vigia-logs-<AccountId>-us-east-1"
        )
        prefixes.add(str(attributes["access_logs.s3.prefix"]))
    return prefixes


@pytest.mark.parametrize("deployment_name", ["pilot", "pilot-passthrough"])
def test_logs_policy_accepts_the_load_balancers(deployment_name: str) -> None:
    """``vigia-alb-app`` y ``vigia-alb-nodes`` (o el balanceador de red de R2) entregan en sus
    prefijos: la cuenta de Elastic Load Balancing de us-east-1 y ``delivery.logs``."""
    synthesized = synthesize(**DEPLOYMENTS[deployment_name])
    datasets = synthesized.templates[STACK]
    prefixes = _load_balancer_prefixes(synthesized.templates["vigia-edge"])
    assert prefixes == {"alb/app", "alb/nodes"}
    expected = {
        f"arn:aws:s3:::vigia-logs-<AccountId>-us-east-1/{p}/AWSLogs/<AccountId>/*" for p in prefixes
    }
    elb = {resource for resource, _ in _grants(datasets, {"AWS": ELB_ACCOUNT_ROOT})}
    assert elb == expected
    delivery = list(_grants(datasets, {"Service": "delivery.logs.amazonaws.com"}))
    assert {resource for resource, _ in delivery} == expected
    for _, conditions in delivery:
        assert conditions["StringEquals"]["s3:x-amz-acl"] == "bucket-owner-full-control"


# --- Solo en pilot compartido (D-8) --------------------------------------------------------


def test_datasets_stack_exists_only_in_shared_pilot(deployment: Synthesized) -> None:
    config = deployment.config
    shared_pilot = config.environment == "pilot" and config.instance == "shared"
    found = [name for name in deployment.stack_names if name.startswith(STACK)]
    assert found == ([STACK] if shared_pilot else [])
    assert config.include_datasets is shared_pilot
