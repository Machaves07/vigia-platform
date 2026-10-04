"""Política de firmantes de la planta (LC-GOB-04; DE §2.7; BR-GOB-25).

``put_policy`` (``PUT /plants/{plant_id}/signatory-policy``, ``commissioning.run`` sobre la planta):
planta de la organización y dentro del alcance (si no, ``ResourceNotFound``); ``minimum`` ≥ 3 (si
no, ``fewer_than_three``) y ``copasst`` en ``required_roles`` (si no,
``workers_representation_missing``); roles repetidos o un mínimo fuera del tope,
``SignatoryPolicyInvalid`` (``invalid_request``). En una transacción, la fila de la planta (se
inserta o se actualiza) y su entrada de auditoría ``signatory_policy_changed``: la política es una
proyección sin tipo de registro y esa entrada es su rastro (fallo cerrado).

``policy`` (``GET``, ``catalog.read`` sobre la planta): la política o ``None``. Bajo concesión,
auditada en la misma transacción (A-56).

La política no bloquea nada por sí sola: la usa el alta del acuerdo (``catalog.agreements``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.agreements import (
    AgreementRuleViolated,
    SignatoryPolicy,
    check_policy,
)
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import Role, ScopeContext, repository

__all__ = ["SignatoryPolicyInvalid", "SignatoryPolicyService", "rejected"]


class SignatoryPolicyInvalid(Exception):
    """Roles repetidos o desconocidos, o un mínimo fuera del tope: ``invalid_request``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str = "política de firmantes fuera de los límites") -> None:
        super().__init__(reason)


def rejected(violation: AgreementRuleViolated) -> CatalogRejected:
    """La violación del dominio con su ``detail_code`` (``catalog_<nombre>``)."""
    return CatalogRejected(CatalogDetailCode(f"catalog_{violation.violation.value}"))


@repository
class SignatoryPolicyService:
    """``catalog.agreements`` (política de firmantes): fijar y leer la de una planta."""

    def __init__(
        self,
        *,
        repository: PostgresAgreementRepository,
        plants: PostgresPlantPolicyRepository,
        database: LedgerDatabase,
        authorizer: Authorizer,
        audit: AuditWriter,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._plants = plants
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock

    def __repr__(self) -> str:
        return "SignatoryPolicyService()"

    async def _plant(
        self, context: ScopeContext, plant_id: uuid.UUID, key: PermissionKey
    ) -> ScopeContext:
        """El contexto autorizado sobre la planta; inexistente o fuera de alcance, igual."""
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        if not await self._plants.plant_exists(context, plant_id):
            raise ResourceNotFound()
        return await self._authorizer.authorize(
            context, key, Resource.plant(context.organization_id, plant_id)
        )

    async def put_policy(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        required_roles: Sequence[object],
        minimum: object,
    ) -> SignatoryPolicy:
        """Fija la política de la planta; la devuelve tras confirmar."""
        authorized = await self._plant(context, plant_id, PermissionKey.COMMISSIONING_RUN)
        if isinstance(required_roles, str | bytes) or type(minimum) is not int:
            raise SignatoryPolicyInvalid()
        try:
            roles = check_policy([Role(str(role)) for role in required_roles], minimum)
        except AgreementRuleViolated as violation:
            raise rejected(violation) from None
        except ValueError:
            raise SignatoryPolicyInvalid() from None
        policy = SignatoryPolicy(
            organization_id=authorized.organization_id,
            plant_id=plant_id,
            required_roles=roles,
            minimum=minimum,
            updated_by=uuid.UUID(str(authorized.actor.id)),
            updated_at=utc_instant(self._clock.now()),
        )
        async with self._database.transaction(authorized) as transaction:
            await self._repository.save_policy(transaction, policy)
            await self._audit.append(
                authorized,
                AuditOperation.SIGNATORY_POLICY_CHANGED,
                plant_id=plant_id,
                filters={"minimum": minimum, "required_roles": [r.value for r in roles]},
                transaction=transaction,
            )
        return policy

    async def policy(self, context: ScopeContext, plant_id: uuid.UUID) -> SignatoryPolicy | None:
        """La política de la planta o ``None``; ``catalog.read`` sobre la planta."""
        authorized = await self._plant(context, plant_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            policy = await self._repository.policy(transaction, plant_id)
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada (fallo cerrado).
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=plant_id,
                    result_count=1,
                    transaction=transaction,
                )
        return policy
