"""Matriz de permisos por rol como código versionado (``domain-entities.md`` §2.6; BR-NUC-15).

Cada operación de la plataforma declara una **clave de permiso** (``PermissionKey``). La matriz
es la de §2.6 (34 claves) con las notas fechadas de U-03 (``agreements.sign``) y U-04
(``metrics.read``, ``exposure.read``, ``commitments.write``, ``vocabulary.manage`` y
``closure_attachments.read``), confirmadas por la adenda A-13: 40 claves por 7 roles. No se
consulta a la base: nada de la matriz puede cambiar sin una versión nueva del código.

Las **prohibiciones por diseño no tienen clave**, así que no existe manera de concederlas: ninguna
clave concede al mando de línea una operación sobre el registro (H-54), al administrador leer
hallazgos ni evidencias (RF-PLA-11), video sin difuminar (P3), editar o borrar un registro del
expediente (P4), exportar etiquetas para reentrenar (RF-PLA-14) ni condicionar la transparencia
del COPASST a una aprobación (H-55). ``tests/properties/test_authz.py`` lo verifica.

Dos reglas no caben en una tabla y las aplica ``identity.authz.authorize``:

- las claves ``platform.*`` solo valen con un contexto de la organización **proveedora** y sin
  concesión;
- ``platform_operator`` y ``provider_installer`` solo conceden en la proveedora, salvo el
  ``provider_installer`` bajo concesión, que recibe **exactamente** su columna sobre el alcance
  concedido (BR-NUC-37).

Módulo puro: no importa FastAPI ni SQLAlchemy (NFR-NUC-25).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from vigia_platform.shared.context import AllowedScope, Role

__all__ = [
    "MATRIX",
    "PLATFORM_PREFIX",
    "PermissionKey",
    "effective_permissions",
    "is_platform_key",
    "permission_key",
    "permissions_of",
    "roles_with",
]

PLATFORM_PREFIX: Final = "platform."


class PermissionKey(enum.StrEnum):
    """Las claves de permiso registradas; una ruta que exige otra no arranca (BR-NUC-15)."""

    # U-04: el lazo de acreditación.
    FINDINGS_READ = "findings.read"
    FINDINGS_CLASSIFY = "findings.classify"
    REVIEW_QUEUE_RESOLVE = "review_queue.resolve"
    ACTIONS_MANAGE = "actions.manage"
    FINDINGS_CLOSE_AND_SIGN = "findings.close_and_sign"
    ROOT_CAUSE_ANALYZE = "root_cause.analyze"
    REVIEW_PACKAGE_READ = "review_package.read"
    EXECUTIVE_SUMMARY_READ = "executive_summary.read"
    EXPORT_CREATE = "export.create"
    # U-02: el núcleo.
    COVERAGE_READ = "coverage.read"
    EVIDENCE_READ = "evidence.read"
    LABELS_READ = "labels.read"
    INTEGRITY_VERIFY = "integrity.verify"
    # U-03: gobernanza y flota.
    CATALOG_MANAGE = "catalog.manage"
    CATALOG_READ = "catalog.read"
    COMMISSIONING_RUN = "commissioning.run"
    FLEET_READ = "fleet.read"
    FLEET_MANAGE = "fleet.manage"
    # U-02.
    HEALTH_READ = "health.read"
    LIVE_VIEW_OPEN = "live_view.open"
    # U-03 y U-05.
    TRANSPARENCY_READ = "transparency.read"
    # U-02: usuarios, jerarquía, organización, concesiones y auditoría.
    USERS_MANAGE = "users.manage"
    ROLES_MANAGE = "roles.manage"
    HIERARCHY_MANAGE = "hierarchy.manage"
    HIERARCHY_READ = "hierarchy.read"
    ORGANIZATION_SETTINGS = "organization.settings"
    CONCESSIONS_READ = "concessions.read"
    CONCESSIONS_REVOKE = "concessions.revoke"
    CONCESSIONS_GRANT = "concessions.grant"
    AUDIT_READ = "audit.read"
    # U-04.
    NOTIFICATIONS_READ = "notifications.read"
    # Orden administrativa y operación (solo la organización proveedora).
    PLATFORM_ORGANIZATIONS_CREATE = "platform.organizations.create"
    PLATFORM_KEYS_ROTATE = "platform.keys.rotate"
    PLATFORM_DEAD_LETTER_REPLAY = "platform.dead_letter.replay"
    # Nota fechada de U-03 (2026-09-20) y adenda A-13.
    AGREEMENTS_SIGN = "agreements.sign"
    # Nota fechada de U-04 (2026-09-20) y adenda A-13.
    METRICS_READ = "metrics.read"
    EXPOSURE_READ = "exposure.read"
    COMMITMENTS_WRITE = "commitments.write"
    VOCABULARY_MANAGE = "vocabulary.manage"
    CLOSURE_ATTACHMENTS_READ = "closure_attachments.read"


_K = PermissionKey
_COORDINATOR = Role.COORDINATOR_SST
_LINE = Role.LINE_MANAGER
_PLANT = Role.PLANT_MANAGER
_ADMIN = Role.ADMINISTRATOR
_INSTALLER = Role.PROVIDER_INSTALLER
_COPASST = Role.COPASST
_OPERATOR = Role.PLATFORM_OPERATOR
_ALL = tuple(Role)

_GRANTS: Final[Mapping[PermissionKey, tuple[Role, ...]]] = {
    _K.FINDINGS_READ: (_COORDINATOR, _PLANT),
    _K.FINDINGS_CLASSIFY: (_COORDINATOR,),
    _K.REVIEW_QUEUE_RESOLVE: (_COORDINATOR,),
    _K.ACTIONS_MANAGE: (_COORDINATOR,),
    _K.FINDINGS_CLOSE_AND_SIGN: (_COORDINATOR,),
    _K.ROOT_CAUSE_ANALYZE: (_COORDINATOR,),
    _K.REVIEW_PACKAGE_READ: (_COORDINATOR, _LINE, _PLANT),
    _K.EXECUTIVE_SUMMARY_READ: (_COORDINATOR, _PLANT),
    _K.EXPORT_CREATE: (_COORDINATOR, _PLANT),
    _K.COVERAGE_READ: (_COORDINATOR, _LINE, _PLANT, _ADMIN, _INSTALLER, _COPASST),
    _K.EVIDENCE_READ: (_COORDINATOR, _PLANT),
    _K.LABELS_READ: (_COORDINATOR, _PLANT),
    _K.INTEGRITY_VERIFY: (_COORDINATOR, _PLANT, _ADMIN, _OPERATOR),
    _K.CATALOG_MANAGE: (_ADMIN,),
    _K.CATALOG_READ: (_COORDINATOR, _LINE, _PLANT, _ADMIN, _INSTALLER, _COPASST),
    _K.COMMISSIONING_RUN: (_INSTALLER,),
    _K.FLEET_READ: (_ADMIN, _INSTALLER, _OPERATOR),
    _K.FLEET_MANAGE: (_INSTALLER,),
    _K.HEALTH_READ: (_ADMIN, _OPERATOR),
    _K.LIVE_VIEW_OPEN: (_COORDINATOR, _ADMIN, _INSTALLER, _COPASST),
    _K.TRANSPARENCY_READ: (_COORDINATOR, _LINE, _PLANT, _ADMIN, _INSTALLER, _COPASST),
    # platform_operator: solo en la proveedora (el rol solo se asigna allí, BR-NUC-11).
    _K.USERS_MANAGE: (_ADMIN, _OPERATOR),
    _K.ROLES_MANAGE: (_ADMIN, _OPERATOR),
    _K.HIERARCHY_MANAGE: (_ADMIN,),
    _K.HIERARCHY_READ: _ALL,
    _K.ORGANIZATION_SETTINGS: (_PLANT, _ADMIN),
    _K.CONCESSIONS_READ: (_PLANT, _ADMIN, _OPERATOR),
    _K.CONCESSIONS_REVOKE: (_PLANT, _ADMIN, _OPERATOR),
    _K.CONCESSIONS_GRANT: (_INSTALLER, _OPERATOR),
    _K.AUDIT_READ: (_PLANT, _ADMIN, _OPERATOR),
    _K.NOTIFICATIONS_READ: _ALL,
    _K.PLATFORM_ORGANIZATIONS_CREATE: (_OPERATOR,),
    _K.PLATFORM_KEYS_ROTATE: (_OPERATOR,),
    _K.PLATFORM_DEAD_LETTER_REPLAY: (_OPERATOR,),
    _K.AGREEMENTS_SIGN: (_COORDINATOR, _PLANT, _ADMIN, _INSTALLER),
    _K.METRICS_READ: (_COORDINATOR, _PLANT),
    _K.EXPOSURE_READ: (_COORDINATOR, _PLANT, _ADMIN),
    _K.COMMITMENTS_WRITE: (_LINE, _COORDINATOR),
    _K.VOCABULARY_MANAGE: (_ADMIN,),
    _K.CLOSURE_ATTACHMENTS_READ: (_COORDINATOR, _PLANT),
}


def _by_role() -> Mapping[Role, frozenset[PermissionKey]]:
    if set(_GRANTS) != set(PermissionKey):
        raise RuntimeError("toda clave de permiso tiene su fila en la matriz")
    return MappingProxyType(
        {role: frozenset(key for key, roles in _GRANTS.items() if role in roles) for role in Role}
    )


MATRIX: Final[Mapping[Role, frozenset[PermissionKey]]] = _by_role()
"""Las claves de cada rol (la columna del rol en §2.6). Inmutable."""


def permission_key(value: object) -> PermissionKey:
    """``value`` como clave registrada; ``ValueError`` si no lo es (BR-NUC-15)."""
    if isinstance(value, PermissionKey):
        return value
    if isinstance(value, str):
        try:
            return PermissionKey(value)
        except ValueError:
            pass
    raise ValueError("clave de permiso no registrada en la matriz")


def is_platform_key(key: PermissionKey) -> bool:
    """``platform.*``: solo en la organización proveedora."""
    return key.value.startswith(PLATFORM_PREFIX)


def permissions_of(role: Role) -> frozenset[PermissionKey]:
    """La columna de ``role``."""
    return MATRIX[Role(role)]


def roles_with(key: PermissionKey) -> frozenset[Role]:
    """Los roles cuya columna tiene ``key``."""
    key = permission_key(key)
    return frozenset(role for role, keys in MATRIX.items() if key in keys)


def effective_permissions(scopes: Iterable[AllowedScope]) -> frozenset[PermissionKey]:
    """Unión de las columnas de las asignaciones (BR-NUC-13), sin mirar el alcance."""
    result: frozenset[PermissionKey] = frozenset()
    for scope in scopes:
        result |= MATRIX[scope.role]
    return result
