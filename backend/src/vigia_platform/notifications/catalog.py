"""Catálogo de eventos de las notificaciones (TASK-324; LC-LAZ-30; domain-entities.md de U-04 §6.1;
business-logic-model.md de U-04 §7.2 con sus notas U04-H-06 y U04-H-13; BR-LAZ-124, 125, 127 y
132; respuesta 16).

Una fila por evento que notifica, de U-02, de U-03 y de U-04: urgencia (``immediate | normal |
digest``), canales, audiencia (``client | provider | both``), regla de destinatarios por rol y
alcance, si es silenciable (**nunca** ``immediate``, BR-LAZ-126) y clave de plantilla. Es
configuración de U-04, no dato de cliente: vive en el código, versionada.

Las notas de la tabla, tal como mandan sobre el texto de arriba:

- **D-10** (U04-H-06): ``fleet_alarm_raised`` y ``fleet_alarm_cleared`` van **solo al proveedor**
  (audiencia ``provider``); sus urgencias ``[hipótesis]`` no cambian.
- **U04-H-13**: ``user_invited`` **no está**: la invitación la envía U-02 por ``EmailSenderPort``.
- **U04-H-12**: las urgencias ``[hipótesis]`` de los eventos de U-03 rigen tal como están escritas
  (``hypothesis = True``): ``gate_state_changed`` es ``immediate`` con ``status = revoked`` y
  ``fleet_alarm_raised`` con ``node_mute`` o ``simulated_adapter_in_productive``
  (``immediate_when``); ``normal`` en el resto.
- ``integrity_compromised`` va a los dos (``both``) y su correo **no depende del cortacircuito**
  (``bypasses_email_circuit``, BR-LAZ-127).

Los destinatarios del proveedor son roles de la organización proveedora, que solo admite
``platform_operator`` y ``provider_installer`` (``identity.application.roles.PROVIDER_ROLES``):
«operador de plataforma» y «administrador de la proveedora» son ``platform_operator`` (el
``administrator`` no es asignable en la proveedora), y «proveedor» a secas (alarmas de flota, copia
de ``adoption_low``, integridad) son los dos roles (``PROVIDER_STAFF``).
«Responsable de la acción» y «solicitante» son personas que salen del evento o de su registro
(``PersonRef``); también ellas tienen que tener un rol con alcance sobre la zona o planta.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from vigia_platform.loop.domain.enums import (
    NotificationAudience,
    NotificationChannel,
    NotificationUrgency,
)
from vigia_platform.shared.context import ActorUnit, Role

__all__ = [
    "CATALOG",
    "CLIENT_ROLES",
    "PROVIDER_STAFF",
    "CatalogEntry",
    "ImmediateWhen",
    "PersonRef",
    "RecipientRule",
    "catalog_entry",
    "subscribed_events",
]

_A: Final = NotificationAudience
_U: Final = NotificationUrgency
IN_APP: Final = (NotificationChannel.IN_APP,)
WITH_EMAIL: Final = (NotificationChannel.IN_APP, NotificationChannel.EMAIL)
"""«aplicación, correo» o «aplicación, correo agrupado»: el agrupado es el correo de ``normal``."""

COORDINATOR: Final = Role.COORDINATOR_SST
PLANT_MANAGER: Final = Role.PLANT_MANAGER
LINE_MANAGER: Final = Role.LINE_MANAGER
ADMINISTRATOR: Final = Role.ADMINISTRATOR
OPERATOR: Final = Role.PLATFORM_OPERATOR
PROVIDER_STAFF: Final = (Role.PLATFORM_OPERATOR, Role.PROVIDER_INSTALLER)
"""«Proveedor» sin más: los operadores y los instaladores de la organización proveedora."""
CLIENT_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.LINE_MANAGER,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.COPASST,
)
"""Los roles de una organización cliente: una persona del evento solo recibe el aviso si tiene
alguno de ellos con alcance sobre la zona o planta (BR-LAZ-124)."""


class PersonRef(enum.StrEnum):
    """Una persona que sale del evento, no de un rol."""

    ASSIGNEE = "assignee"
    """El responsable de la acción: ``assignee_user_id`` de la carga o la acción nueva de la
    reapertura (``new_action_id``)."""
    REQUESTER = "requester"
    """El solicitante de la exportación: ``requested_by`` del registro ``export_produced``."""


@dataclass(frozen=True, slots=True)
class RecipientRule:
    """Quién recibe el aviso: roles con alcance que contenga la zona o planta del evento."""

    client: tuple[Role, ...] = ()
    """Roles de la organización del evento."""
    persons: tuple[PersonRef, ...] = ()
    client_fallback: tuple[Role, ...] = ()
    """Si ningún usuario de ``client`` tiene alcance, estos (``adoption_low``: el gerente de
    planta cuando no hay coordinador activo, BR-LAZ-137)."""
    provider: tuple[Role, ...] = ()
    """Roles de la organización proveedora (alcance: toda la proveedora)."""


@dataclass(frozen=True, slots=True)
class ImmediateWhen:
    """La urgencia sube a ``immediate`` cuando ``payload[field]`` es uno de ``values``."""

    field: str
    values: frozenset[str]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """``NotificationEventCatalog`` (domain-entities §6.1)."""

    event_name: str
    source_unit: ActorUnit
    urgency: NotificationUrgency
    channels: tuple[NotificationChannel, ...]
    recipients: RecipientRule
    audience: NotificationAudience
    template_key: str
    description_es: str
    immediate_when: ImmediateWhen | None = None
    hypothesis: bool = False
    """Urgencia ``[hipótesis]`` de U-04 sobre un evento de U-03 (U04-H-12)."""
    bypasses_email_circuit: bool = False
    """El correo se intenta siempre, sin mirar el cortacircuito (BR-LAZ-127)."""

    def urgency_for(self, payload: Mapping[str, object]) -> NotificationUrgency:
        rule = self.immediate_when
        if rule is not None and payload.get(rule.field) in rule.values:
            return NotificationUrgency.IMMEDIATE
        return self.urgency

    def silenceable(self, payload: Mapping[str, object]) -> bool:
        """Falso para ``immediate`` (BR-LAZ-126): ninguna preferencia lo silencia."""
        return self.urgency_for(payload) is not NotificationUrgency.IMMEDIATE

    @property
    def emails(self) -> bool:
        return NotificationChannel.EMAIL in self.channels


def _entry(
    event_name: str,
    source_unit: ActorUnit,
    urgency: NotificationUrgency,
    channels: tuple[NotificationChannel, ...],
    recipients: RecipientRule,
    description_es: str,
    *,
    audience: NotificationAudience = NotificationAudience.CLIENT,
    template_key: str | None = None,
    immediate_when: ImmediateWhen | None = None,
    hypothesis: bool = False,
    bypasses_email_circuit: bool = False,
) -> CatalogEntry:
    return CatalogEntry(
        event_name=event_name,
        source_unit=source_unit,
        urgency=urgency,
        channels=channels,
        recipients=recipients,
        audience=audience,
        template_key=template_key or event_name,
        description_es=description_es,
        immediate_when=immediate_when,
        hypothesis=hypothesis,
        bypasses_email_circuit=bypasses_email_circuit,
    )


_U02: Final = ActorUnit.U02
_U03: Final = ActorUnit.U03
_U04: Final = ActorUnit.U04
_COORDINATOR: Final = RecipientRule(client=(COORDINATOR,))
_COORDINATOR_AND_MANAGER: Final = RecipientRule(client=(COORDINATOR, PLANT_MANAGER))
_COORDINATOR_AND_ADMINISTRATOR: Final = RecipientRule(client=(COORDINATOR, ADMINISTRATOR))
_ADMINISTRATOR: Final = RecipientRule(client=(ADMINISTRATOR,))
_PROVIDER: Final = RecipientRule(provider=PROVIDER_STAFF)
_OPERATOR: Final = RecipientRule(provider=(OPERATOR,))

_ENTRIES: Final = (
    # --- U-03: gobernanza y flota (§8.4) -------------------------------------------------------
    _entry("finding_received", _U03, _U.NORMAL, WITH_EMAIL, _COORDINATOR, "Hallazgo nuevo"),
    _entry(
        "detection_for_review_received",
        _U03,
        _U.NORMAL,
        WITH_EMAIL,
        _COORDINATOR,
        "Detección nueva en la cola de revisión",
    ),
    # --- U-04: lazo -------------------------------------------------------------------------------
    _entry(
        "finding_confirmed",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        _COORDINATOR_AND_MANAGER,
        "Hallazgo confirmado",
    ),
    _entry("finding_discarded", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Hallazgo descartado"),
    _entry("finding_reclassified", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Hallazgo reclasificado"),
    _entry("loop_suspended", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Lazo suspendido"),
    _entry("loop_resumed", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Lazo reanudado"),
    _entry("review_confirmed", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Revisión confirmada"),
    _entry("review_discarded", _U04, _U.NORMAL, IN_APP, _COORDINATOR, "Revisión descartada"),
    _entry("review_overdue", _U04, _U.IMMEDIATE, WITH_EMAIL, _COORDINATOR, "Revisión vencida"),
    _entry(
        "action_assigned",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        RecipientRule(client=(COORDINATOR,), persons=(PersonRef.ASSIGNEE,)),
        "Acción correctiva asignada",
    ),
    _entry(
        "action_evidence_added",
        _U04,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR,
        "Evidencia añadida a una acción",
    ),
    _entry(
        "finding_closed",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        _COORDINATOR_AND_MANAGER,
        "Hallazgo cerrado",
    ),
    _entry(
        "finding_unclosable",
        _U04,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _COORDINATOR_AND_MANAGER,
        "Hallazgo declarado sin alternativa segura",
    ),
    _entry(
        "unclosable_review_due",
        _U04,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _COORDINATOR_AND_MANAGER,
        "Revisión de un incerrable pendiente",
    ),
    _entry(
        "unclosable_reviewed",
        _U04,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR_AND_MANAGER,
        "Incerrable revisado",
    ),
    _entry(
        "finding_reopened",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        RecipientRule(client=(COORDINATOR,), persons=(PersonRef.ASSIGNEE,)),
        "Hallazgo reabierto",
    ),
    _entry(
        "finding_overdue",
        _U04,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(client=(COORDINATOR,), persons=(PersonRef.ASSIGNEE,)),
        "Acción vencida",
    ),
    # El nivel manager_weekly entra además en el digest del gerente: lo compone TASK-348.
    _entry("finding_escalated", _U04, _U.IMMEDIATE, WITH_EMAIL, _COORDINATOR, "Vencido escalado"),
    _entry(
        "root_cause_analysis_signed",
        _U04,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR,
        "Análisis de causa raíz firmado",
    ),
    # --- U-04: acreditación ---------------------------------------------------------------------
    _entry(
        "export_available",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        RecipientRule(persons=(PersonRef.REQUESTER,)),
        "Exportación lista",
    ),
    _entry(
        "monthly_package_available",
        _U04,
        _U.NORMAL,
        WITH_EMAIL,
        RecipientRule(client=(LINE_MANAGER, COORDINATOR, PLANT_MANAGER)),
        "Paquete mensual disponible",
    ),
    _entry(
        "commitments_recorded",
        _U04,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR_AND_MANAGER,
        "Compromisos registrados",
    ),
    _entry(
        "adoption_low",
        _U04,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(
            client=(COORDINATOR,), client_fallback=(PLANT_MANAGER,), provider=PROVIDER_STAFF
        ),
        "Zona activa sin actividad del lazo",
        audience=_A.BOTH,
    ),
    _entry(
        "vocabulary_list_changed",
        _U04,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Lista de vocabulario cambiada",
    ),
    _entry(
        "late_segment_attached",
        _U04,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR,
        "Tramo tardío añadido a un episodio",
    ),
    _entry(
        "loop_projection_divergence",
        _U04,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _OPERATOR,
        "Divergencia de la proyección del lazo",
        audience=_A.PROVIDER,
    ),
    # --- U-03: urgencias [hipótesis] de U-04 (U04-H-12) -----------------------------------------
    _entry(
        "zone_activated",
        _U03,
        _U.NORMAL,
        WITH_EMAIL,
        _COORDINATOR_AND_ADMINISTRATOR,
        "Zona activada",
        hypothesis=True,
    ),
    _entry(
        "catalog_updated",
        _U03,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR_AND_ADMINISTRATOR,
        "Catálogo de la zona actualizado",
        hypothesis=True,
    ),
    _entry(
        "gate_state_changed",
        _U03,
        _U.NORMAL,
        WITH_EMAIL,
        RecipientRule(client=(COORDINATOR, PLANT_MANAGER, ADMINISTRATOR)),
        "Compuerta de la zona cambiada",
        immediate_when=ImmediateWhen("status", frozenset({"revoked"})),
        hypothesis=True,
    ),
    _entry(
        "regression_marked",
        _U03,
        _U.NORMAL,
        WITH_EMAIL,
        _COORDINATOR_AND_ADMINISTRATOR,
        "Acta de la zona no vigente",
        hypothesis=True,
    ),
    _entry(
        "regression_cleared",
        _U03,
        _U.NORMAL,
        IN_APP,
        _COORDINATOR_AND_ADMINISTRATOR,
        "Acta de la zona vigente de nuevo",
        hypothesis=True,
    ),
    _entry(
        "node_enrolled",
        _U03,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Nodo dado de alta",
        template_key="node_changed",
        hypothesis=True,
    ),
    _entry(
        "node_revoked",
        _U03,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Nodo revocado",
        template_key="node_changed",
        hypothesis=True,
    ),
    _entry(
        "node_decommissioned",
        _U03,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Nodo retirado",
        template_key="node_changed",
        hypothesis=True,
    ),
    _entry(
        "target_version_published",
        _U03,
        _U.DIGEST,
        IN_APP,
        _ADMINISTRATOR,
        "Versión objetivo publicada",
        template_key="node_update",
        hypothesis=True,
    ),
    _entry(
        "update_result_received",
        _U03,
        _U.DIGEST,
        IN_APP,
        _ADMINISTRATOR,
        "Resultado de actualización recibido",
        template_key="node_update",
        hypothesis=True,
    ),
    # D-10 (U04-H-06): las alarmas de flota, solo al proveedor.
    _entry(
        "fleet_alarm_raised",
        _U03,
        _U.NORMAL,
        WITH_EMAIL,
        _PROVIDER,
        "Alarma de flota",
        audience=_A.PROVIDER,
        immediate_when=ImmediateWhen(
            "alarm_kind", frozenset({"node_mute", "simulated_adapter_in_productive"})
        ),
        hypothesis=True,
    ),
    _entry(
        "fleet_alarm_cleared",
        _U03,
        _U.NORMAL,
        IN_APP,
        _PROVIDER,
        "Alarma de flota resuelta",
        audience=_A.PROVIDER,
        hypothesis=True,
    ),
    # --- U-02: núcleo (user_invited no está: U04-H-13) --------------------------------------------
    _entry(
        "user_activated",
        _U02,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Usuario activado",
        template_key="user_changed",
    ),
    _entry(
        "user_deactivated",
        _U02,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Usuario desactivado",
        template_key="user_changed",
    ),
    _entry(
        "role_assignment_changed",
        _U02,
        _U.NORMAL,
        IN_APP,
        _ADMINISTRATOR,
        "Asignación de rol cambiada",
        template_key="user_changed",
    ),
    # BR-LAZ-132: ningún acceso del proveedor es invisible para el cliente.
    _entry(
        "concession_granted",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(client=(ADMINISTRATOR, PLANT_MANAGER)),
        "Concesión concedida al proveedor",
    ),
    _entry(
        "concession_revoked",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(client=(ADMINISTRATOR, PLANT_MANAGER)),
        "Concesión del proveedor revocada",
        template_key="concession_ended",
    ),
    _entry(
        "concession_expired",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(client=(ADMINISTRATOR, PLANT_MANAGER)),
        "Concesión del proveedor vencida",
        template_key="concession_ended",
    ),
    _entry(
        "security_alert",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _OPERATOR,
        "Alerta de seguridad",
        audience=_A.PROVIDER,
    ),
    _entry(
        "dead_letter_created",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _OPERATOR,
        "Entrega en cola muerta",
        audience=_A.PROVIDER,
    ),
    _entry(
        "key_rotation_due",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        _OPERATOR,
        "Rotación de clave pendiente",
        audience=_A.PROVIDER,
    ),
    _entry(
        "integrity_compromised",
        _U02,
        _U.IMMEDIATE,
        WITH_EMAIL,
        RecipientRule(client=(ADMINISTRATOR,), provider=PROVIDER_STAFF),
        "Integridad del expediente comprometida",
        audience=_A.BOTH,
        bypasses_email_circuit=True,
    ),
    _entry(
        "checkpoint_written",
        _U02,
        _U.DIGEST,
        IN_APP,
        _OPERATOR,
        "Punto de control escrito",
        audience=_A.PROVIDER,
        template_key="ledger_maintenance",
    ),
    _entry(
        "key_set_published",
        _U02,
        _U.DIGEST,
        IN_APP,
        _OPERATOR,
        "Juego de claves publicado",
        audience=_A.PROVIDER,
        template_key="ledger_maintenance",
    ),
)

CATALOG: Final[Mapping[str, CatalogEntry]] = {entry.event_name: entry for entry in _ENTRIES}
"""``fila(event_name)``: el catálogo completo, por nombre de evento."""

if len(CATALOG) != len(_ENTRIES):  # pragma: no cover - defecto del propio catálogo
    raise RuntimeError("evento repetido en el catálogo de notificaciones")


def catalog_entry(event_name: str) -> CatalogEntry | None:
    """La fila del evento, o ``None`` si el evento no notifica."""
    return CATALOG.get(event_name)


def subscribed_events() -> tuple[str, ...]:
    """Los eventos a los que se suscribe ``notification_engine``, en el orden del catálogo."""
    return tuple(CATALOG)
