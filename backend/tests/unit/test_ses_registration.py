"""``notifications.registration._register_email_sender``: ``SesEmailSender`` solo en ``pilot``
con el dominio configurado; en cualquier otro entorno, ``FakeEmailSender`` (TASK-325; LC-LAZ-33).

Una credencial real (``email_sender_domain`` configurado) fuera de ``pilot`` **nunca** llega a
registrar el adaptador real: la prueba de arranque es justamente esta selección por entorno, que
corre en los tres procesos (``api``, ``worker`` y ``admin``) a través de ``outbox_catalog``.
``EMAIL_SENDERS`` es la ranura de todo el proceso: cada prueba la sustituye por una vacía propia
para no depender del orden de ejecución de las demás.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest

from vigia_platform.identity.application.invitations import EmailSenderRegistry
from vigia_platform.notifications import registration
from vigia_platform.notifications.adapters.ses.fake import FakeEmailSender
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.outbox.registries import ConsumerRegistry
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.units import UnitServices

_BASE = {
    "aws_region": "us-east-1",
    "db_app_secret": "vigia/pilot/db/app",
    "signing_secret_prefix": "vigia/pilot/signing/",
    "secrets_key_arn": "alias/vigia-secrets",
}


def _services(config: RuntimeConfig | None) -> UnitServices:
    inert: Any = Mock()
    return UnitServices(
        clock=SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        metrics=Mock(),
        provider_organization_id=uuid.uuid4(),
        database=inert,
        contexts=inert,
        authorizer=inert,
        audit=inert,
        outbox=inert,
        writer=inert,
        free_text=inert,
        signing=inert,
        checkpoints=inert,
        kms=inert,
        config=config,
    )


@pytest.fixture(autouse=True)
def _fresh_email_senders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registration, "EMAIL_SENDERS", EmailSenderRegistry())


def _register(config: RuntimeConfig | None) -> None:
    registration._register_email_sender(ConsumerRegistry(), _services(config))


def test_without_config_the_default_is_fake() -> None:
    _register(None)
    assert isinstance(registration.EMAIL_SENDERS.sender, FakeEmailSender)


@pytest.mark.parametrize("environment", ["local", "test", "staging-1", "staging-12"])
def test_outside_pilot_the_default_is_fake_even_with_a_real_looking_credential(
    environment: str,
) -> None:
    config = RuntimeConfig(environment=environment, email_sender_domain="mail.vigia.test", **_BASE)
    _register(config)
    assert isinstance(registration.EMAIL_SENDERS.sender, FakeEmailSender)


def test_in_pilot_without_the_sender_domain_the_default_is_still_fake() -> None:
    config = RuntimeConfig(environment="pilot", **_BASE)
    _register(config)
    assert isinstance(registration.EMAIL_SENDERS.sender, FakeEmailSender)


def test_in_pilot_with_the_sender_domain_the_real_adapter_registers() -> None:
    config = RuntimeConfig(environment="pilot", email_sender_domain="mail.vigia.test", **_BASE)
    _register(config)
    assert isinstance(registration.EMAIL_SENDERS.sender, SesEmailSender)


def test_registering_twice_in_the_same_process_is_idempotent() -> None:
    config = RuntimeConfig(environment="pilot", email_sender_domain="mail.vigia.test", **_BASE)
    _register(config)
    first = registration.EMAIL_SENDERS.sender
    _register(config)
    assert registration.EMAIL_SENDERS.sender is first
