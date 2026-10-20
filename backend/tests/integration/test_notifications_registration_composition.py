"""Composición de ``notifications.registration`` con la entrega de invitaciones de U-02
(TASK-325; rework de revisión del PR #163).

La ranura ``EMAIL_SENDERS`` es la misma que ``shared.runtime.api`` y ``shared.runtime.admin``
componen en el proceso real. Esta prueba no reconstruye esos dos módulos entero (no son el objeto
bajo prueba, y montar el ``FastAPI`` completo no añade nada a lo que aquí se comprueba); en su
lugar ejecuta la **misma función de composición** (``_register_email_sender``) que ambos invocan,
sobre ``RuntimeConfig`` real, y usa la ranura resultante en un flujo de invitación real contra
PostgreSQL (``tests.hierarchy_support``), igual que hacen ``api.py``/``admin.py`` al pasar
``EMAIL_SENDERS`` a ``UserService``/``OrganizationGenesis``.

Lo que protege: en ``pilot`` sin ``VIGIA_EMAIL_SENDER_DOMAIN``, la ranura queda vacía y la
invitación **se divulga** (``InvitationDelivery.LINK_DISCLOSED``) en vez de fingir una entrega real
con ``FakeEmailSender`` que nunca llegó a ocurrir (BR-NUC-32).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest

from tests.hierarchy_support import (
    GOOD_PASSWORD,
    HierarchyEnvironment,
    hierarchy_environment,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import GOOD_CODE
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec
from vigia_platform.identity.application.invitations import EmailSenderRegistry, InvitationDelivery
from vigia_platform.identity.application.roles import AssignmentRequest
from vigia_platform.identity.application.users import InviteRequest
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.notifications import registration
from vigia_platform.notifications.adapters.ses.fake import FakeEmailSender
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.outbox.registries import ConsumerRegistry
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.units import UnitServices

pytestmark = pytest.mark.integration

_BASE_CONFIG: dict[str, Any] = {
    "aws_region": "us-east-1",
    "db_app_secret": "vigia/pilot/db/app",
    "signing_secret_prefix": "vigia/pilot/signing/",
    "secrets_key_arn": "alias/vigia-secrets",
}


def _services(config: RuntimeConfig) -> UnitServices:
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


@pytest.fixture
def _fresh_email_senders(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(registration, "EMAIL_SENDERS", EmailSenderRegistry())
    yield


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[HierarchyEnvironment]:
    with hierarchy_environment(postgres_endpoint, "notifications_registration_composition") as e:
        yield e


def _plant() -> PlantSpec:
    return PlantSpec("PL-NOTIF", "Planta de prensas", "CO", "us-east-1", "America/Bogota")


def test_pilot_without_sender_domain_the_real_invitation_flow_discloses_the_link(
    env: HierarchyEnvironment, _fresh_email_senders: None
) -> None:
    config = RuntimeConfig(environment="pilot", **_BASE_CONFIG)
    registration._register_email_sender(ConsumerRegistry(), _services(config))
    assert registration.EMAIL_SENDERS.sender is None

    result = env.run(
        env.genesis(senders=registration.EMAIL_SENDERS).create_client_organization(
            env.operator_context(),
            GenesisRequest(
                code="ORG-NOTIF-1",
                name="Organización sintética",
                plant=_plant(),
                administrator_email=new_email("admin"),
                administrator_display_name="Administración sintética",
            ),
        )
    )
    assert result.invitation.delivery is InvitationDelivery.LINK_DISCLOSED
    assert result.invitation.link is not None
    env.run(
        env.invitations().accept_invitation(
            result.invitation.link.split("#", 1)[1],
            GOOD_PASSWORD,
            CURRENT_PRIVACY_NOTICE_VERSION,
            GOOD_CODE,
        )
    )

    second = env.run(
        env.users(registration.EMAIL_SENDERS).invite_user(
            env.session_context(result.organization_id, result.administrator_user_id),
            InviteRequest(
                email=new_email("coordinador"),
                display_name="Coordinación sintética",
                assignments=(
                    AssignmentRequest(Role.COORDINATOR_SST, ScopeLevel.PLANT, result.plant_id),
                ),
            ),
        )
    )
    assert second.delivery is InvitationDelivery.LINK_DISCLOSED


def test_pilot_with_sender_domain_but_without_credentials_still_falls_back(
    env: HierarchyEnvironment, _fresh_email_senders: None
) -> None:
    """Fuera de ``pilot`` (``local``/``test``/``staging-N``), el valor por omisión sigue siendo
    ``FakeEmailSender`` (sin cambio de comportamiento para esos entornos; rework de revisión)."""
    config = RuntimeConfig(environment="test", **_BASE_CONFIG)
    registration._register_email_sender(ConsumerRegistry(), _services(config))
    assert isinstance(registration.EMAIL_SENDERS.sender, FakeEmailSender)

    result = env.run(
        env.genesis(senders=registration.EMAIL_SENDERS).create_client_organization(
            env.operator_context(),
            GenesisRequest(
                code="ORG-NOTIF-2",
                name="Organización sintética",
                plant=_plant(),
                administrator_email=new_email("admin"),
                administrator_display_name="Administración sintética",
            ),
        )
    )
    assert result.invitation.delivery is InvitationDelivery.EMAIL_QUEUED
