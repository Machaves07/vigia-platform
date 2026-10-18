"""Pila ``vigia-loop`` de U-04 (TASK-331, parte 1 de 2): pruebas de plantilla, solo ``cdk synth``.

Infrastructure-design de U-04 §2.2 (pila e importaciones por el contrato nº 38), §2.3 (pruebas
bloqueantes 1, 2, 3, 4, 6 y 7; la 5, de alarmas, llega con ellas), §3.2 (punto privado y
``sg-queue-endpoint``), §5 (tema, cola y cola muerta), §8 (clave y secreto) y §9.1 (política
``vigia-loop-worker``, sin ``ses:SendEmail`` hasta TASK-351). A-47: nada toca una cuenta de AWS.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template

import app as cdk_app
from config import EnvironmentConfig
from stacks import register_stacks
from stacks.loop import (
    CONSUME_ACTIONS,
    ENDPOINT_ACTIONS,
    LoopStack,
    assistant_secret_name,
    email_events_dlq_name,
    email_events_name,
    loop_worker_policy_name,
    queue_endpoint_name,
)
from tests.conftest import DEPLOYMENTS, Synthesized, default_context, synthesize
from tests.template_rules import (
    RESOURCE_WILDCARDS,
    check_policy_wildcards,
    describe,
    properties,
    render,
    resources,
)

JsonObject = Mapping[str, Any]
REQUIRED_TAGS = ("project", "unit", "managed_by", "environment")
KEY = "AWS::KMS::Key"
TOPIC = "AWS::SNS::Topic"
QUEUE = "AWS::SQS::Queue"
SECRET = "AWS::SecretsManager::Secret"  # noqa: S105 - tipo de recurso, no una contraseña
POLICY = "AWS::IAM::ManagedPolicy"
ENDPOINT = "AWS::EC2::VPCEndpoint"
GROUP = "AWS::EC2::SecurityGroup"
TLS_DENIED = {"Bool": {"aws:SecureTransport": "false"}}
# Lo que conserva ``RETAIN`` en ``pilot`` (deployment-architecture de U-04 §4) y la clave.
RETAINED_TYPES = (KEY, TOPIC, QUEUE, SECRET)
# Las pilas de U-02 que se comparan antes y después de añadir ``vigia-loop``.
COMPARED = ("pilot", "staging-7", "dedicated-acme")


def expected_loop_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-loop`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    expected: Counter[str] = Counter(
        {
            KEY: 1,
            "AWS::KMS::Alias": 1,
            TOPIC: 1,
            "AWS::SNS::TopicPolicy": 1,
            "AWS::SNS::Subscription": 1,
            QUEUE: 2,
            "AWS::SQS::QueuePolicy": 2,
            GROUP: 1,
            "AWS::EC2::SecurityGroupIngress": 1,
            ENDPOINT: 1,
            POLICY: 1,
        }
    )
    if config.loop.assistant_secret:
        expected += Counter({SECRET: 1, "AWS::SecretsManager::ResourcePolicy": 1})
    return expected


# --- Lectura de la plantilla ----------------------------------------------------------


def _loop(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("loop")]


def _of_type(template: JsonObject, kind: str) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        if resource["Type"] == kind:
            yield logical_id, resource


def _one(template: JsonObject, kind: str) -> tuple[str, JsonObject]:
    (found,) = _of_type(template, kind)
    return found


def _parameter(template: JsonObject, name: str) -> dict[str, str]:
    """``Ref`` al parámetro SSM de despliegue ``name`` (``/vigia/<despliegue>/<recurso>``)."""
    (logical_id,) = (
        logical_id
        for logical_id, parameter in template["Parameters"].items()
        if parameter.get("Default") == name
    )
    assert template["Parameters"][logical_id]["Type"] == "AWS::SSM::Parameter::Value<String>"
    return {"Ref": logical_id}


def _queues(template: JsonObject) -> dict[str, str]:
    """Nombre de la cola → identificador lógico."""
    return {
        str(properties(r)["QueueName"]): logical_id for logical_id, r in _of_type(template, QUEUE)
    }


def _queue_policy(template: JsonObject, queue: str) -> list[JsonObject]:
    (statements,) = (
        properties(r)["PolicyDocument"]["Statement"]
        for _, r in _of_type(template, "AWS::SQS::QueuePolicy")
        if properties(r)["Queues"] == [{"Ref": queue}]
    )
    return list(statements)


def _arn(logical_id: str) -> dict[str, list[str]]:
    return {"Fn::GetAtt": [logical_id, "Arn"]}


def _tags(resource: JsonObject) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in properties(resource).get("Tags", [])}


def _allows(statements: list[JsonObject]) -> list[JsonObject]:
    return [s for s in statements if s["Effect"] == "Allow"]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


@pytest.fixture
def loop(deployment: Synthesized) -> tuple[Synthesized, JsonObject]:
    """Cada despliegue de :data:`DEPLOYMENTS` con su plantilla de ``vigia-loop``."""
    return deployment, _loop(deployment)


# --- La pila: registro, contrato y ninguna huella en las pilas de U-02 ----------------


def _u02_only(**context: Any) -> dict[str, JsonObject]:
    """Las plantillas de U-02 sin ``vigia-loop``: lo que ``app.build`` hacía antes de TASK-331."""
    application = App(context={**default_context(), **context}, analytics_reporting=True)
    config = cdk_app.read_config(application)
    register_stacks(application, config, tags=cdk_app.global_tags(config))
    cdk_app.apply_tags(application, config)
    assembly = application.synth()
    return {stack.stack_name: stack.template for stack in assembly.stacks}


@pytest.mark.parametrize("name", COMPARED)
def test_loop_emits_no_resource_of_another_stack(name: str) -> None:
    """Criterio 1: los identificadores lógicos (y las plantillas enteras) de las pilas de U-02 son
    los mismos antes y después de añadir ``vigia-loop``."""
    before = _u02_only(**DEPLOYMENTS[name])
    after = synthesize(**DEPLOYMENTS[name])
    loop_name = after.config.stack_name("loop")
    assert set(after.stack_names) == {*before, loop_name}
    assert after.stack_names[-1] == loop_name
    for stack, template in before.items():
        assert set(after.templates[stack]["Resources"]) == set(template["Resources"]), stack
        assert json.dumps(after.templates[stack], sort_keys=True) == json.dumps(
            template, sort_keys=True
        ), stack
    loop_ids = set(after.templates[loop_name]["Resources"]) - {"CDKMetadata"}
    for stack, template in before.items():
        assert not loop_ids & set(template["Resources"]), stack


def test_loop_reads_u02_only_through_the_contract(loop: tuple[Synthesized, JsonObject]) -> None:
    deployment, template = loop
    config = deployment.config
    assert "Fn::ImportValue" not in json.dumps(template)
    assert deployment.missing_context == ()
    defaults = {p.get("Default") for p in template["Parameters"].values()}
    prefix = f"/vigia/{config.deployment}"
    assert defaults == {
        "/cdk-bootstrap/vigia/version",
        *(
            f"{prefix}/{name}"
            for name in (
                "worker-task-role-arn",
                "vpc-id",
                "app-subnet-a-id",
                "app-subnet-b-id",
                "sg-worker-id",
                "evidence-key-arn",
                # ``vigia-secrets`` solo cifra el secreto del asistente, ausente en staging.
                *(("secrets-key-arn",) if config.loop.assistant_secret else ()),
            )
        ),
    }
    # Ningún parámetro importado queda sin referencia (W2001 de la validación).
    text = json.dumps(template["Resources"])
    for logical_id in template["Parameters"]:
        assert logical_id == "BootstrapVersion" or logical_id in text, logical_id


def test_loop_goes_last_after_the_four_stacks_it_imports(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, _ = loop
    config = deployment.config
    assert deployment.stack_names[-1] == config.stack_name("loop")
    assert set(deployment.dependencies[config.stack_name("loop")]) == {
        config.stack_name(key) for key in ("foundation", "data", "edge", "compute")
    }


def test_loop_has_its_expected_resources(loop: tuple[Synthesized, JsonObject]) -> None:
    deployment, template = loop
    found = Counter(str(r["Type"]) for _, r in resources(template))
    assert found == expected_loop_resources(deployment.config)


# --- §2.3 nº 1: colas con clave del cliente -------------------------------------------


def test_queues_and_topic_use_the_queue_key(loop: tuple[Synthesized, JsonObject]) -> None:
    _, template = loop
    key, _ = _one(template, KEY)
    queues = list(_of_type(template, QUEUE))
    assert len(queues) == 2
    for logical_id, queue in queues:
        assert properties(queue)["KmsMasterKeyId"] == _arn(key), logical_id
    _, topic = _one(template, TOPIC)
    assert properties(topic)["KmsMasterKeyId"] == _arn(key)


# --- §2.3 nº 2: cola muerta con ``maxReceiveCount`` -----------------------------------


def test_every_queue_redrives_to_a_dead_letter_queue_after_5(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    queues = _queues(template)
    main = queues[email_events_name(deployment.config)]
    dlq = queues[email_events_dlq_name(deployment.config)]
    redrives = {
        logical_id: properties(q).get("RedrivePolicy")
        for logical_id, q in _of_type(template, QUEUE)
    }
    targets = {
        str(r["deadLetterTargetArn"]["Fn::GetAtt"][0]) for r in redrives.values() if r is not None
    }
    # Toda cola que no es la cola muerta de otra redirige a una, con el máximo declarado.
    assert targets == {dlq}
    for logical_id, redrive in redrives.items():
        if logical_id in targets:
            continue
        assert redrive == {"deadLetterTargetArn": _arn(dlq), "maxReceiveCount": 5}, logical_id
    assert set(redrives) == {main, dlq}


# --- §2.3 nº 3: ninguna política de U-04 con comodín ----------------------------------


def test_no_loop_policy_has_an_unlisted_wildcard(loop: tuple[Synthesized, JsonObject]) -> None:
    deployment, template = loop
    violations = check_policy_wildcards(deployment.config.stack_name("loop"), template)
    assert not violations, describe(violations)


def _loop_documents(template: JsonObject) -> Iterator[tuple[str, JsonObject]]:
    kinds = {
        POLICY: "PolicyDocument",
        "AWS::SNS::TopicPolicy": "PolicyDocument",
        "AWS::SQS::QueuePolicy": "PolicyDocument",
        "AWS::SecretsManager::ResourcePolicy": "ResourcePolicy",
        KEY: "KeyPolicy",
        ENDPOINT: "PolicyDocument",
    }
    for logical_id, resource in resources(template):
        key = kinds.get(str(resource["Type"]))
        if key:
            yield logical_id, properties(resource)[key]


def test_loop_allows_name_actions_and_resources_without_wildcards(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    """Más estricto que SECURITY-06: ninguna acción con comodín en ninguna sentencia ``Allow``,
    ``Resource: "*"`` solo en las políticas del propio recurso (clave y secreto) y, como único
    comodín de recurso, los dos prefijos de paquetes de §9.1."""
    _, template = loop
    packages = next(w for w in RESOURCE_WILDCARDS if w.name == "evidence-package-prefixes")
    for logical_id, document in _loop_documents(template):
        kind = template["Resources"][logical_id]["Type"]
        for statement in _allows(_as_list(document["Statement"])):
            for action in _as_list(statement["Action"]):
                assert "*" not in action and "?" not in action, (logical_id, action)
            for raw in _as_list(statement["Resource"]):
                text = render(raw, template)
                if text == "*":
                    assert kind in {KEY, "AWS::SecretsManager::ResourcePolicy"}, logical_id
                elif "*" in text or "?" in text:
                    assert kind == POLICY and packages.pattern.match(text), (logical_id, text)
            assert statement.get("Principal") not in ("*", {"AWS": "*"}), logical_id


def test_wildcard_probe_detects_a_wildcard_action_in_the_loop_policy() -> None:
    """La regla falla si una sentencia de ``vigia-loop-worker`` usa un comodín."""
    config = _config()
    application = App(context=default_context())
    stack = LoopStack(application, config, tags=cdk_app.global_tags(config))
    stack.loop_worker_policy.add_statements(
        iam.PolicyStatement(actions=["sqs:*"], resources=[stack.email_events_queue.queue_arn])
    )
    template = dict(Template.from_stack(stack).to_json())
    violations = check_policy_wildcards("vigia-loop", template)
    assert any("sqs:*" in v.detail for v in violations), describe(violations)


# --- §2.3 nº 4: las cuatro etiquetas con ``unit=U-04`` --------------------------------


def test_every_loop_resource_carries_the_four_tags_with_u04(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    config = deployment.config
    expected = {
        "project": "vigia",
        "unit": "U-04",
        "managed_by": "cdk",
        "environment": config.environment,
    }
    # Las etiquetas de la pila, que CloudFormation propaga a todo recurso que las admite.
    assert deployment.tags[config.stack_name("loop")] == expected
    tagged = {str(r["Type"]) for _, r in resources(template) if "Tags" in properties(r)}
    assert tagged >= {KEY, TOPIC, QUEUE, GROUP, ENDPOINT}
    for logical_id, resource in resources(template):
        if "Tags" in properties(resource):
            tags = _tags(resource)
            assert {k: tags.get(k) for k in REQUIRED_TAGS} == expected, logical_id


# --- §2.3 nº 6: ``vigia-api`` no referencia el secreto --------------------------------


def test_api_task_definition_does_not_reference_the_assistant_secret(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    """La plantilla de ``vigia-compute`` se lee tal cual sale de la síntesis, sin modificarla."""
    deployment, _ = loop
    config = deployment.config
    compute = deployment.templates[config.stack_name("compute")]
    (api,) = (
        r
        for _, r in _of_type(compute, "AWS::ECS::TaskDefinition")
        if str(properties(r)["Family"]).startswith("vigia-api")
    )
    text = json.dumps(api)
    assert assistant_secret_name(config) not in text
    assert "assistant" not in text
    (api_policy,) = (
        r
        for _, r in _of_type(compute, POLICY)
        if properties(r)["ManagedPolicyName"] == config.resource_name("api-task")
    )
    assert "assistant" not in json.dumps(api_policy)


# --- §2.3 nº 7: tema y colas deniegan sin TLS -----------------------------------------


def _denies_insecure_transport(statements: list[JsonObject], action: str) -> bool:
    return any(
        s["Effect"] == "Deny"
        and s.get("Principal") in ("*", {"AWS": "*"})
        and s.get("Condition") == TLS_DENIED
        and action in _as_list(s["Action"])
        for s in statements
    )


def test_topic_and_queues_deny_requests_without_tls(loop: tuple[Synthesized, JsonObject]) -> None:
    _, template = loop
    topic, _ = _one(template, TOPIC)
    _, topic_policy = _one(template, "AWS::SNS::TopicPolicy")
    assert properties(topic_policy)["Topics"] == [{"Ref": topic}]
    assert _denies_insecure_transport(
        list(properties(topic_policy)["PolicyDocument"]["Statement"]), "sns:Publish"
    )
    for queue in _queues(template).values():
        assert _denies_insecure_transport(_queue_policy(template, queue), "sqs:*"), queue


# --- §5: tema, cola, cola muerta y suscripción ----------------------------------------


def test_queue_parameters_follow_section_5(loop: tuple[Synthesized, JsonObject]) -> None:
    deployment, template = loop
    queues = _queues(template)
    resources_by_id = template["Resources"]
    main = properties(resources_by_id[queues[email_events_name(deployment.config)]])
    assert main["MessageRetentionPeriod"] == 14 * 24 * 3600
    assert main["VisibilityTimeout"] == 60
    assert main["ReceiveMessageWaitTimeSeconds"] == 20
    assert "FifoQueue" not in main  # cola estándar
    dlq = properties(resources_by_id[queues[email_events_dlq_name(deployment.config)]])
    assert dlq["MessageRetentionPeriod"] == 14 * 24 * 3600
    assert "RedrivePolicy" not in dlq


def test_queue_policy_admits_the_topic_and_the_worker_only(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    config = deployment.config
    topic, _ = _one(template, TOPIC)
    worker = _parameter(template, f"/vigia/{config.deployment}/worker-task-role-arn")
    queues = _queues(template)
    main = queues[email_events_name(config)]
    dlq = queues[email_events_dlq_name(config)]
    consume = {
        "Sid": "WorkerConsumes",
        "Effect": "Allow",
        "Principal": {"AWS": worker},
        "Action": list(CONSUME_ACTIONS),
        "Resource": _arn(main),
    }
    assert _normalized(_allows(_queue_policy(template, main))) == _normalized(
        [
            consume,
            {
                "Sid": "TopicDelivers",
                "Effect": "Allow",
                "Principal": {"Service": "sns.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": _arn(main),
                "Condition": {"ArnEquals": {"aws:SourceArn": {"Ref": topic}}},
            },
        ]
    )
    assert _normalized(_allows(_queue_policy(template, dlq))) == _normalized(
        [{**consume, "Resource": _arn(dlq)}]
    )


def test_topic_admits_publications_only_from_the_email_service_of_the_account(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    topic, resource = _one(template, TOPIC)
    assert properties(resource)["TopicName"] == email_events_name(deployment.config)
    _, policy = _one(template, "AWS::SNS::TopicPolicy")
    assert _allows(list(properties(policy)["PolicyDocument"]["Statement"])) == [
        {
            "Sid": "EmailServicePublishes",
            "Effect": "Allow",
            "Principal": {"Service": "ses.amazonaws.com"},
            "Action": "sns:Publish",
            "Resource": {"Ref": topic},
            "Condition": {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}},
        }
    ]


def test_only_subscription_is_the_queue_with_raw_delivery(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    topic, _ = _one(template, TOPIC)
    main = _queues(template)[email_events_name(deployment.config)]
    _, subscription = _one(template, "AWS::SNS::Subscription")
    assert properties(subscription) == {
        "Endpoint": _arn(main),
        "Protocol": "sqs",
        "RawMessageDelivery": True,
        "TopicArn": {"Ref": topic},
    }


# --- §8.1: clave ``vigia-queue`` ------------------------------------------------------


def _key_statements(template: JsonObject) -> dict[str, JsonObject]:
    _, key = _one(template, KEY)
    return {s["Sid"]: s for s in properties(key)["KeyPolicy"]["Statement"]}


def test_queue_key_rotates_yearly_with_an_explicit_policy(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    config = deployment.config
    key_id, key = _one(template, KEY)
    props = properties(key)
    assert props["EnableKeyRotation"] is True
    assert props["RotationPeriodInDays"] == 365
    assert props["PendingWindowInDays"] == (7 if config.ephemeral else 30)
    _, alias = _one(template, "AWS::KMS::Alias")
    assert properties(alias) == {
        "AliasName": f"alias/{config.resource_name('queue')}",
        "TargetKeyId": _arn(key_id),
    }
    statements = _key_statements(template)
    assert set(statements) == {
        "KeyAdministration",
        "WorkerDecryptsThroughSqs",
        "NotificationsUseTheKey",
        "EmailServiceUsesTheKey",
    }
    administration = statements["KeyAdministration"]
    assert not any(a in _as_list(administration["Action"]) for a in ("kms:Decrypt", "kms:Encrypt"))
    worker = statements["WorkerDecryptsThroughSqs"]
    assert worker["Action"] == "kms:Decrypt"
    assert worker["Condition"] == {
        "ArnEquals": {
            "aws:PrincipalArn": [
                _parameter(template, f"/vigia/{config.deployment}/worker-task-role-arn")
            ]
        },
        "StringEquals": {"kms:ViaService": "sqs.us-east-1.amazonaws.com"},
    }
    for sid, service in (
        ("NotificationsUseTheKey", "sns.amazonaws.com"),
        ("EmailServiceUsesTheKey", "ses.amazonaws.com"),
    ):
        assert statements[sid]["Principal"] == {"Service": service}
        assert sorted(_as_list(statements[sid]["Action"])) == ["kms:Decrypt", "kms:GenerateDataKey"]
        assert statements[sid]["Condition"] == {
            "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
        }


def _config(**context: Any) -> EnvironmentConfig:
    application = App(context={**default_context(), **context})
    return cdk_app.read_config(application)


def _loop_alone(config: EnvironmentConfig) -> JsonObject:
    """``vigia-loop`` sin las pilas de U-02: solo lee parámetros, así que se sintetiza sola."""
    application = App(context=default_context())
    stack = LoopStack(application, config, tags=cdk_app.global_tags(config))
    return dict(Template.from_stack(stack).to_json())


def test_h_laz_9_switch_moves_only_the_topic_to_the_service_key() -> None:
    """Contingencia de §5: el tema pasa a ``alias/aws/sns`` y la cola conserva ``vigia-queue``;
    el servicio de correo deja de usar la clave del cliente."""
    config = _config()
    switched = replace(config, loop=replace(config.loop, email_topic_service_managed_key=True))
    template = _loop_alone(switched)
    key, _ = _one(template, KEY)
    _, topic = _one(template, TOPIC)
    assert render(properties(topic)["KmsMasterKeyId"], template) == (
        "arn:aws:kms:us-east-1:<AccountId>:alias/aws/sns"
    )
    for _, queue in _of_type(template, QUEUE):
        assert properties(queue)["KmsMasterKeyId"] == _arn(key)
    assert set(_key_statements(template)) == {
        "KeyAdministration",
        "WorkerDecryptsThroughSqs",
        "NotificationsUseTheKey",
    }


def test_h_laz_9_switch_is_off_in_both_environments() -> None:
    assert not _config().loop.email_topic_service_managed_key
    assert not _config(environment="staging-1").loop.email_topic_service_managed_key


# --- §3.2: punto privado y ``sg-queue-endpoint`` --------------------------------------


def test_queue_endpoint_sits_in_both_app_subnets_with_a_scoped_policy(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    config = deployment.config
    prefix = f"/vigia/{config.deployment}"
    group, _ = _one(template, GROUP)
    _, endpoint = _one(template, ENDPOINT)
    props = properties(endpoint)
    queues = _queues(template)
    assert props["ServiceName"] == "com.amazonaws.us-east-1.sqs"
    assert props["VpcEndpointType"] == "Interface"
    assert props["PrivateDnsEnabled"] is True
    assert props["VpcId"] == _parameter(template, f"{prefix}/vpc-id")
    assert props["SubnetIds"] == [
        _parameter(template, f"{prefix}/app-subnet-a-id"),
        _parameter(template, f"{prefix}/app-subnet-b-id"),
    ]
    assert props["SecurityGroupIds"] == [{"Fn::GetAtt": [group, "GroupId"]}]
    assert _tags(endpoint)["Name"] == queue_endpoint_name(config)
    assert _normalized(props["PolicyDocument"]["Statement"]) == _normalized(
        [
            {
                "Sid": "WorkerUsesTheEmailEventsQueues",
                "Effect": "Allow",
                "Principal": {"AWS": _parameter(template, f"{prefix}/worker-task-role-arn")},
                "Action": list(ENDPOINT_ACTIONS),
                "Resource": [
                    _arn(queues[email_events_name(config)]),
                    _arn(queues[email_events_dlq_name(config)]),
                ],
            }
        ]
    )


def test_queue_endpoint_group_admits_443_from_sg_worker_only_and_has_no_egress(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    group, resource = _one(template, GROUP)
    props = properties(resource)
    assert props["GroupName"] == "sg-queue-endpoint"
    assert props["VpcId"] == _parameter(template, f"/vigia/{deployment.config.deployment}/vpc-id")
    assert "SecurityGroupIngress" not in props
    # CDK expresa «sin salida» con una regla imposible hacia 255.255.255.255/32.
    assert props["SecurityGroupEgress"] == [
        {
            "CidrIp": "255.255.255.255/32",
            "Description": "Disallow all traffic",
            "FromPort": 252,
            "IpProtocol": "icmp",
            "ToPort": 86,
        }
    ]
    assert not list(_of_type(template, "AWS::EC2::SecurityGroupEgress"))
    _, ingress = _one(template, "AWS::EC2::SecurityGroupIngress")
    assert properties(ingress) == {
        "Description": "443 desde sg-worker",
        "FromPort": 443,
        "ToPort": 443,
        "IpProtocol": "tcp",
        "GroupId": {"Fn::GetAtt": [group, "GroupId"]},
        "SourceSecurityGroupId": _parameter(
            template, f"/vigia/{deployment.config.deployment}/sg-worker-id"
        ),
    }


# --- §8.2: secreto del asistente ------------------------------------------------------


def test_assistant_secret_is_empty_and_readable_only_by_the_worker(pilot: Synthesized) -> None:
    template = _loop(pilot)
    secret, resource = _one(template, SECRET)
    props = properties(resource)
    assert props["Name"] == "vigia/pilot/assistant/api-key"
    assert props["KmsKeyId"] == _parameter(template, "/vigia/pilot/secrets-key-arn")
    assert "SecretString" not in props and "GenerateSecretString" not in props
    _, policy = _one(template, "AWS::SecretsManager::ResourcePolicy")
    assert properties(policy)["SecretId"] == {"Ref": secret}
    statements = {s["Sid"]: s for s in properties(policy)["ResourcePolicy"]["Statement"]}
    worker = _parameter(template, "/vigia/pilot/worker-task-role-arn")
    assert _allows(list(statements.values())) == [
        {
            "Sid": "WorkerReads",
            "Effect": "Allow",
            "Principal": {"AWS": worker},
            "Action": "secretsmanager:GetSecretValue",
            "Resource": "*",
        }
    ]
    others = statements["NobodyElseReads"]
    assert (others["Effect"], others["Principal"], others["Action"]) == (
        "Deny",
        {"AWS": "*"},
        "secretsmanager:GetSecretValue",
    )
    assert others["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": worker}}
    deletion = statements["OnlyAdministrationDeletes"]
    assert (deletion["Effect"], deletion["Action"]) == ("Deny", "secretsmanager:DeleteSecret")
    assert render(deletion["Condition"]["ArnNotEquals"]["aws:PrincipalArn"], template) == (
        "arn:aws:iam::<AccountId>:role/cdk-vigia-cfn-exec-role-<AccountId>-us-east-1"
    )


def test_assistant_secret_is_absent_in_staging(staging: Synthesized) -> None:
    template = _loop(staging)
    assert not list(_of_type(template, SECRET))
    assert not list(_of_type(template, "AWS::SecretsManager::ResourcePolicy"))
    assert "assistant" not in json.dumps(template)
    assert "secretsmanager" not in json.dumps(template)


# --- §9.1: política gestionada ``vigia-loop-worker`` ----------------------------------


def _expected_loop_worker_document(
    template: JsonObject, config: EnvironmentConfig
) -> dict[str, Any]:
    """La tabla de §9.1, sentencia por sentencia, sin ``ses:SendEmail`` (TASK-351)."""
    prefix = f"/vigia/{config.deployment}"
    key, _ = _one(template, KEY)
    queues = _queues(template)
    main = _arn(queues[email_events_name(config)])
    dlq = _arn(queues[email_events_dlq_name(config)])
    bucket = [
        f"arn:aws:s3:::vigia-evidence{config.name_suffix}-",
        {"Ref": "AWS::AccountId"},
        "-us-east-1",
    ]

    def objects(path: str) -> dict[str, Any]:
        return {"Fn::Join": ["", [*bucket[:2], f"{bucket[2]}/{path}"]]}

    packages = [objects("org/*/export/*"), objects("org/*/monthly/*")]
    statements: list[dict[str, Any]] = [
        {
            "Sid": "WritePackages",
            "Effect": "Allow",
            "Action": ["s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:PutObject"],
            "Resource": packages,
            "Condition": {
                "StringEquals": {
                    "s3:x-amz-server-side-encryption": "aws:kms",
                    "s3:x-amz-server-side-encryption-aws-kms-key-id": _parameter(
                        template, f"{prefix}/evidence-key-arn"
                    ),
                }
            },
        },
        {
            "Sid": "ListMultipartUploads",
            "Effect": "Allow",
            "Action": "s3:ListBucketMultipartUploads",
            "Resource": {"Fn::Join": ["", bucket]},
            "Condition": {"StringLike": {"s3:prefix": "org/*"}},
        },
        {
            "Sid": "ReadOwnPackages",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": packages,
        },
        {
            "Sid": "ConsumeEmailEvents",
            "Effect": "Allow",
            "Action": sorted(CONSUME_ACTIONS),
            "Resource": sorted([main, dlq], key=json.dumps),
        },
        {
            "Sid": "RedriveFromDeadLetterQueue",
            "Effect": "Allow",
            "Action": "sqs:SendMessage",
            "Resource": main,
        },
        {
            "Sid": "QueueKeyThroughSqs",
            "Effect": "Allow",
            "Action": "kms:Decrypt",
            "Resource": _arn(key),
            "Condition": {"StringEquals": {"kms:ViaService": "sqs.us-east-1.amazonaws.com"}},
        },
    ]
    if config.loop.assistant_secret:
        secret, _ = _one(template, SECRET)
        statements.append(
            {
                "Sid": "ReadAssistantSecret",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": {"Ref": secret},
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _normalized(document: Any) -> Any:
    """Listas de acciones y recursos sin orden: CDK las ordena a su manera."""
    if isinstance(document, Mapping):
        return {
            key: (
                sorted((_normalized(v) for v in value), key=json.dumps)
                if key in {"Action", "Resource"} and isinstance(value, list)
                else _normalized(value)
            )
            for key, value in document.items()
        }
    if isinstance(document, list):
        return [_normalized(item) for item in document]
    return document


def test_loop_worker_policy_equals_the_table_of_section_9_1(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    deployment, template = loop
    config = deployment.config
    _, policy = _one(template, POLICY)
    props = properties(policy)
    assert props["ManagedPolicyName"] == loop_worker_policy_name(config)
    assert _normalized(props["PolicyDocument"]) == _normalized(
        _expected_loop_worker_document(template, config)
    )
    assert "ses:" not in json.dumps(props)


def test_loop_worker_policy_never_reaches_other_prefixes_nor_deletes(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    _, template = loop
    _, policy = _one(template, POLICY)
    text = json.dumps(properties(policy)["PolicyDocument"])
    for forbidden in ("closure/", "node/", "documents/", "Delete", "ses:", "logs:", "sns:"):
        assert forbidden not in text.replace("sqs:DeleteMessage", ""), forbidden


def test_loop_worker_policy_is_attached_to_the_imported_worker_role(
    loop: tuple[Synthesized, JsonObject],
) -> None:
    """El rol llega por el ARN que publica ``vigia-compute``; la política se adjunta con
    ``Roles``, sin ``AWS::IAM::Policy`` ni ningún cambio sobre el rol."""
    deployment, template = loop
    _, policy = _one(template, POLICY)
    arn = _parameter(template, f"/vigia/{deployment.config.deployment}/worker-task-role-arn")
    assert properties(policy)["Roles"] == [
        {"Fn::Select": [1, {"Fn::Split": ["/", {"Fn::Select": [5, {"Fn::Split": [":", arn]}]}]}]}
    ]
    assert not list(_of_type(template, "AWS::IAM::Policy"))
    assert not list(_of_type(template, "AWS::IAM::Role"))


# --- Retención y nombres --------------------------------------------------------------


def test_topic_queues_secret_and_key_are_retained_in_permanent_deployments(
    permanent_deployment: Synthesized,
) -> None:
    template = _loop(permanent_deployment)
    for kind in RETAINED_TYPES:
        for logical_id, resource in _of_type(template, kind):
            assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain", (
                logical_id
            )
    assert list(_of_type(template, SECRET))


def test_staging_destroys_the_loop_with_the_environment(staging: Synthesized) -> None:
    template = _loop(staging)
    for kind in RETAINED_TYPES:
        for logical_id, resource in _of_type(template, kind):
            assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Delete", (
                logical_id
            )


# Despliegue → sufijo de los nombres con alcance de cuenta (``pilot`` compartido: los del diseño).
SUFFIXES = {"pilot": "", "staging-7": "-staging-7", "acme": "-acme"}


def test_names_derive_from_environment_and_instance(loop: tuple[Synthesized, JsonObject]) -> None:
    deployment, template = loop
    config = deployment.config
    deployment_name = config.deployment
    suffix = SUFFIXES[deployment_name]
    assert config.stack_name("loop") == f"vigia-loop{suffix}"
    assert set(_queues(template)) == {
        f"vigia-email-events-{deployment_name}",
        f"vigia-email-events-dlq-{deployment_name}",
    }
    _, topic = _one(template, TOPIC)
    assert properties(topic)["TopicName"] == f"vigia-email-events-{deployment_name}"
    _, endpoint = _one(template, ENDPOINT)
    assert _tags(endpoint)["Name"] == f"vigia-vpce-sqs-{deployment_name}"
    _, alias = _one(template, "AWS::KMS::Alias")
    assert properties(alias)["AliasName"] == f"alias/vigia-queue{suffix}"
    _, policy = _one(template, POLICY)
    assert properties(policy)["ManagedPolicyName"] == f"vigia-loop-worker{suffix}"
    secrets = [properties(r)["Name"] for _, r in _of_type(template, SECRET)]
    assert secrets == ([] if config.ephemeral else [f"vigia/{deployment_name}/assistant/api-key"])
