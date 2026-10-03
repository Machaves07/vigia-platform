"""Pilas registradas, su orden, sus dependencias, sus etiquetas y sus recursos esperados.

Infrastructure-design §2.3: seis pilas en el orden ``foundation``, ``data``, ``edge``,
``compute``, ``observability`` y ``datasets``, con las dependencias de su tabla. TASK-144 las
registra vacías; cada tarea de TASK-145 a TASK-150 amplía :data:`EXPECTED_RESOURCES` con
los recursos que añade a su pila.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from aws_cdk import App, Stack
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3

from config import EnvironmentConfig
from stacks import DEPENDENCIES, STACK_ORDER
from tests.conftest import Synthesized, cdk_settings, synthesize
from tests.template_rules import resources
from tests.test_compute import expected_compute_resources
from tests.test_data import expected_data_resources
from tests.test_edge import expected_edge_resources
from tests.test_foundation import expected_foundation_resources
from tests.test_observability import expected_observability_resources

# ``observability`` antes que ``compute``: crea los grupos de registro de sus tareas (TASK-149).
ORDER = ("foundation", "data", "edge", "observability", "compute", "datasets")

# Tipos de recurso y cuántos de cada uno espera cada pila (sin ``AWS::CDK::Metadata``).
EXPECTED_RESOURCES: dict[str, Counter[str]] = {key: Counter() for key in ORDER}
# ``vigia-datasets`` (TASK-150): depósitos del conjunto y de registros, con sus políticas.
EXPECTED_RESOURCES["datasets"] = Counter({"AWS::S3::Bucket": 2, "AWS::S3::BucketPolicy": 2})


def _short(name: str, config: EnvironmentConfig) -> str:
    return name.removeprefix("vigia-").removesuffix(config.name_suffix)


def test_stack_classes_follow_the_design_order() -> None:
    assert tuple(stack.key for stack in STACK_ORDER) == ORDER


def test_pilot_registers_the_six_stacks_in_order(pilot: Synthesized) -> None:
    assert pilot.stack_names == tuple(f"vigia-{key}" for key in ORDER)


def test_dependencies_follow_the_design_table(deployment: Synthesized) -> None:
    config = deployment.config
    for name, dependencies in deployment.dependencies.items():
        expected = {config.stack_name(key) for key in DEPENDENCIES[_short(name, config)]}
        assert set(dependencies) == expected, name
    assert DEPENDENCIES == {
        "foundation": (),
        "data": ("foundation",),
        "edge": ("foundation", "data"),
        "observability": ("foundation", "data", "edge"),
        "compute": ("foundation", "data", "edge", "observability"),
        "datasets": (),
    }


def _expected(key: str, config: EnvironmentConfig) -> Counter[str]:
    """``vigia-foundation`` (presupuestos, traducciones), ``vigia-data`` (depósito de registros
    y vaciado automático), ``vigia-edge`` (zona, cortafuegos, almacén de confianza) y
    ``vigia-compute`` (registro, escalado, permisos del arranque) cambian con el despliegue."""
    if key == "foundation":
        return expected_foundation_resources(config)
    if key == "data":
        return expected_data_resources(config)
    if key == "edge":
        return expected_edge_resources(config)
    if key == "compute":
        return expected_compute_resources(config)
    if key == "observability":
        return expected_observability_resources(config)
    return EXPECTED_RESOURCES[key]


def test_each_stack_has_its_expected_resources(deployment: Synthesized) -> None:
    for name, template in deployment.templates.items():
        found = Counter(str(resource["Type"]) for _, resource in resources(template))
        assert found == _expected(_short(name, deployment.config), deployment.config), name


def test_stacks_are_in_us_east_1_with_the_vigia_qualifier(deployment: Synthesized) -> None:
    for name, template in deployment.templates.items():
        assert deployment.regions[name] == "us-east-1"
        bootstrap = template["Parameters"]["BootstrapVersion"]["Default"]
        assert bootstrap == "/cdk-bootstrap/vigia/version", name
        assert template["Description"].startswith(f"Vigia U-02 ({deployment.config.deployment}):")


def test_stacks_carry_the_global_tags(deployment: Synthesized) -> None:
    for name, tags in deployment.tags.items():
        assert tags == {
            "project": "vigia",
            "unit": "U-02",
            "managed_by": "cdk",
            "environment": deployment.config.environment,
        }, name


def test_resources_inherit_the_global_tags() -> None:
    """Un recurso que TASK-146 añada a ``vigia-data`` recibe las cuatro etiquetas."""

    def add_bucket(application: App, config: EnvironmentConfig) -> None:
        data = application.node.find_child(config.stack_name("data"))
        assert isinstance(data, Stack)
        s3.Bucket(data, "TagProbe", encryption_key=kms.Key(data, "TagProbeKey"))

    synthesized = synthesize(add_bucket, environment="staging-7")
    template = synthesized.templates["vigia-data-staging-7"]
    bucket = next(
        r
        for logical_id, r in resources(template)
        if r["Type"] == "AWS::S3::Bucket" and logical_id.startswith("TagProbe")
    )
    tags = {tag["Key"]: tag["Value"] for tag in bucket["Properties"]["Tags"]}
    assert tags == {
        "project": "vigia",
        "unit": "U-02",
        "managed_by": "cdk",
        "environment": "staging-7",
    }


def test_synthesis_needs_no_aws_lookups(deployment: Synthesized) -> None:
    """Sin ``from_lookup``: nada queda pendiente de consultar a AWS."""
    assert deployment.missing_context == ()


def test_no_stack_couples_to_another_through_exports(deployment: Synthesized) -> None:
    """Las pilas se leen por el contrato de ``stacks/outputs.py``, no por ``Fn::ImportValue``."""
    for name, template in deployment.templates.items():
        assert "Fn::ImportValue" not in str(template), name
        outputs: dict[str, Any] = template.get("Outputs", {})
        assert not any("Export" in output for output in outputs.values()), name


def test_contract_parameters_are_published_once(deployment: Synthesized) -> None:
    names = [
        str(resource["Properties"]["Name"])
        for template in deployment.templates.values()
        for _, resource in resources(template)
        if resource["Type"] == "AWS::SSM::Parameter"
    ]
    assert len(names) == len(set(names))


def test_cdk_json_fixes_the_qualifier_and_default_context() -> None:
    settings = cdk_settings()
    context = settings["context"]
    assert settings["app"] == "uv run --frozen python app.py"
    assert settings["toolkitStackName"] == "CDKToolkit-vigia"
    assert context["@aws-cdk/core:bootstrapQualifier"] == "vigia"
    assert context["environment"] == "pilot"
    assert context["instance"] == "shared"
    assert context["first_deploy"] is False
    assert context["ca_rotation"] is False
    assert context["nat_per_az"] is False
    assert context["nodes_tls_mode"] == "mtls"
