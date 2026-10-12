"""Datos generados a escala: volumetría (NFR-NUC-05) y bancos de NFR-NUC-01 (TASK-142, VIG-91).

Lo comparten ``tests/volumetry/generate_scale_data.py`` (la volumetría a escala objetivo) y los
bancos de ``tests/benchmarks/`` (la lista con alcance sobre un año y la línea de tiempo con el
volumen máximo por zona). Todo como superusuario sobre una base migrada desechable:

- **Identidad**: organizaciones con su usuario, plantas, zonas, un nodo por zona con su
  asignación vigente y la proyección ``ledger.communication_state`` del nodo.
- **Particiones de meses pasados**: ``shared.vigia_create_month_partitions`` (nuc_0016) con el
  actor ``system``, la misma función que usa la tarea semanal.
- **Historia con el disparador real** (``historical_stamps``): el disparador de encadenado toma
  la marca de ``clock_timestamp()``, así que sin más todo el año caería en el mes en curso. Solo
  mientras se genera, la función ``ledger.vigia_chain_link`` se sustituye por una copia idéntica
  salvo en **una línea**: la marca se toma de la fila (``received_at`` o ``occurred_at``) y nunca
  retrocede en la cadena. Secuencia, ``content_hash``, ``record_hash``/``entry_hash``, claves de
  idempotencia e identidades los sigue calculando el disparador, así que las cadenas son íntegras y
  verificables. Al salir se restaura la definición original y se comprueba que es idéntica.
- **Contenido**: hallazgos ``finding_received`` del generador del kit de U-01 (localizados a la
  zona, con ``finding_id`` y clave de idempotencia únicos), eventos de observabilidad con la forma
  de ``ObservabilityEventRecord`` en pares apertura y cierre, clasificaciones (forma mínima de
  U-04), cambios de compuerta y de comunicación del nodo; entradas de auditoría ``ledger_read`` y
  ``coverage_read`` con filtros canónicos.

Ritmos por zona y día ``[estimación propia]`` (``Rates``): 77 hallazgos (RNF-DES-03), 40 eventos de
observabilidad y 37 clasificaciones, unos 154 registros: el orden de los 20 millones al año de
NFR-NUC-03 a escala objetivo. La **zona caliente** lleva el volumen máximo de eventos del diseño
(«menos de 20 000 eventos por zona y mes», plan de NFR Design de U-02): 646 por día.

Determinista para una semilla. Solo datos generados: ningún dato real.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from pydantic import JsonValue
from vigia_contracts.canonical import canonicalize
from vigia_contracts.conformance.generators import finding, zone_catalog

__all__ = [
    "FINDING_TYPE",
    "OBSERVABILITY_TYPE",
    "OrganizationRef",
    "PlantRef",
    "Rates",
    "ScaleWriter",
    "ZoneRef",
    "create_partitions",
    "historical_stamps",
    "kit_findings",
    "month_floor",
    "seed_organization",
    "stamp",
]

FINDING_TYPE: Final = "finding_received"
OBSERVABILITY_TYPE: Final = "observability_event_received"
CLASSIFICATION_TYPE: Final = "classification_probe"
GATE_TYPE: Final = "gate_state_changed"
COMMUNICATION_TYPE: Final = "node_communication_state_changed"

DAY: Final = timedelta(days=1)

_LIVE_STAMP: Final = (
    "taken_at := greatest(date_trunc('milliseconds', clock_timestamp()), head.updated_at);"
)
_HISTORICAL_STAMP: Final = (
    "taken_at := greatest(date_trunc('milliseconds', coalesce(routed_at, clock_timestamp())),"
    " head.updated_at);"
)
_SENTINEL: Final = "00000000-0000-4000-8000-0000000000aa"
"""``finding_id`` de la plantilla; cada fila lo sustituye por uno propio de la misma longitud."""
_CLIP_EPOCH: Final = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Rates:
    """Registros por zona y día ``[estimación propia]``; los eventos van en pares."""

    findings: int = 77
    observability_pairs: int = 20
    classifications: int = 37
    hot_zone_pairs: int = 323
    """Pares de la zona caliente: 646 eventos al día, unos 20 000 en 31 días."""
    communication_pairs: int = 0
    """Pares ``mute``/``reachable`` de ``node_communication_state_changed`` por nodo y día (la
    volumetría de U-03, TASK-233: la historia que recorre ``GET /fleet/nodes``)."""

    def per_zone_day(self) -> int:
        return (
            self.findings
            + 2 * self.observability_pairs
            + self.classifications
            + 2 * self.communication_pairs
        )


@dataclass(frozen=True, slots=True)
class ZoneRef:
    zone_id: uuid.UUID
    node_id: uuid.UUID
    camera_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PlantRef:
    plant_id: uuid.UUID
    zones: tuple[ZoneRef, ...]


@dataclass(frozen=True, slots=True)
class OrganizationRef:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    plants: tuple[PlantRef, ...]

    @property
    def zones(self) -> tuple[ZoneRef, ...]:
        return tuple(zone for plant in self.plants for zone in plant.zones)


def stamp(moment: datetime) -> str:
    """Marca ISO 8601 UTC con milisegundos y ``Z``."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def month_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _uuid(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def _uuid7(moment: datetime, rng: random.Random) -> uuid.UUID:
    """UUID v7 con la marca ``moment`` (los registros del nodo son v7)."""
    milliseconds = int(moment.timestamp() * 1000)
    value = (milliseconds << 80) | (0x7 << 76) | (rng.getrandbits(12) << 64)
    value |= (0b10 << 62) | rng.getrandbits(62)
    return uuid.UUID(int=value)


def _code(prefix: str, value: uuid.UUID) -> str:
    return f"{prefix}-{value.hex[:20].upper()}"


# --- Identidad --------------------------------------------------------------------------------


async def seed_organization(
    connection: Any,
    rng: random.Random,
    *,
    plants: int,
    zones_per_plant: int,
    since: datetime,
) -> OrganizationRef:
    """Organización con su usuario, ``plants`` plantas de ``zones_per_plant`` zonas y un nodo
    asignado a cada zona desde ``since`` (como superusuario, en una transacción)."""
    organization_id, user_id = _uuid(rng), _uuid(rng)
    refs: list[PlantRef] = []
    async with connection.transaction():
        await connection.execute(
            "INSERT INTO identity.organization (organization_id, code, name, kind, created_at,"
            " created_by) VALUES ($1, $2, 'Organización sintética', 'client', $3, $4)",
            organization_id,
            _code("ORG", organization_id),
            since,
            user_id,
        )
        await connection.execute(
            "INSERT INTO identity.user_account (user_id, organization_id, email, display_name,"
            " status, created_at) VALUES ($1, $2, $3, 'Persona sintética', 'active', $4)",
            user_id,
            organization_id,
            f"escala-{user_id.hex}@example.test",
            since,
        )
        for _ in range(plants):
            plant_id = _uuid(rng)
            await connection.execute(
                "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
                " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
                " 'Planta sintética', 'CO', 'us-east-1', 'UTC', $4, $5)",
                plant_id,
                organization_id,
                _code("PL", plant_id),
                since,
                user_id,
            )
            zones: list[ZoneRef] = []
            for _ in range(zones_per_plant):
                zone = ZoneRef(_uuid(rng), _uuid(rng), _uuid(rng))
                zones.append(zone)
                await connection.execute(
                    "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
                    " created_at, created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
                    zone.zone_id,
                    organization_id,
                    plant_id,
                    _code("ZN", zone.zone_id),
                    since,
                    user_id,
                )
                await connection.execute(
                    "INSERT INTO identity.node_identity (node_id, organization_id, plant_id,"
                    " code, status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                    zone.node_id,
                    organization_id,
                    plant_id,
                    _code("ND", zone.node_id),
                    since,
                )
                await connection.execute(
                    "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                    " plant_id, zone_id, node_id, assigned_at, assigned_by)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    _uuid(rng),
                    organization_id,
                    plant_id,
                    zone.zone_id,
                    zone.node_id,
                    since,
                    user_id,
                )
            refs.append(PlantRef(plant_id, tuple(zones)))
    return OrganizationRef(organization_id, user_id, tuple(refs))


# --- Particiones e historia -------------------------------------------------------------------


async def create_partitions(connection: Any, first: datetime, last: datetime) -> None:
    """Particiones mensuales de ``first`` a ``last`` con la función de nuc_0016 (actor system)."""
    async with connection.transaction():
        await connection.execute("SELECT set_config('vigia.actor_kind', 'system', true)")
        rows = await connection.fetch(
            "SELECT * FROM shared.vigia_create_month_partitions($1, $2)",
            month_floor(first).date(),
            month_floor(last).date(),
        )
    blocked = [row["partition"] for row in rows if row["blocked"]]
    if blocked:
        raise RuntimeError(
            f"particiones bloqueadas por filas en la partición por defecto: {blocked}"
        )


@contextlib.asynccontextmanager
async def historical_stamps(connection: Any) -> AsyncIterator[None]:
    """Mientras dura, el disparador de encadenado toma la marca de la fila (ver el módulo)."""
    original: str = await connection.fetchval(
        "SELECT pg_get_functiondef('ledger.vigia_chain_link()'::regprocedure)"
    )
    if original.count(_LIVE_STAMP) != 1:
        raise RuntimeError("la línea de la marca de ledger.vigia_chain_link ya no es la esperada")
    await connection.execute(original.replace(_LIVE_STAMP, _HISTORICAL_STAMP))
    try:
        yield
    finally:
        await connection.execute(original)
        restored = await connection.fetchval(
            "SELECT pg_get_functiondef('ledger.vigia_chain_link()'::regprocedure)"
        )
        if restored != original:
            raise RuntimeError("ledger.vigia_chain_link no quedó como estaba")


# --- Contenido --------------------------------------------------------------------------------


def kit_findings(count: int, seed: int) -> list[dict[str, Any]]:
    """``count`` hallazgos del generador del kit de U-01, deterministas para ``seed``."""
    pool: list[dict[str, Any]] = []

    @hypothesis_seed(seed)
    @settings(
        max_examples=count * 4,
        database=None,
        deadline=None,
        phases=[Phase.generate],
        suppress_health_check=list(HealthCheck),
    )
    @given(st.data())
    def draw(data: st.DataObject) -> None:
        pool.append(data.draw(finding(data.draw(zone_catalog()))))

    draw()
    if len(pool) < count:
        raise RuntimeError(f"el kit dio {len(pool)} hallazgos de {count}")
    return pool[:count]


class _Contents:
    """Bytes canónicos del contenido de cada tipo, localizados a la zona."""

    def __init__(self, findings: Sequence[dict[str, Any]], rng: random.Random) -> None:
        self._findings = findings
        self._rng = rng
        self._templates: dict[uuid.UUID, list[bytes]] = {}

    def _zone_templates(
        self, organization_id: uuid.UUID, plant_id: uuid.UUID, zone: ZoneRef
    ) -> list[bytes]:
        templates = self._templates.get(zone.zone_id)
        if templates is None:
            templates = []
            for document in self._rng.sample(list(self._findings), min(8, len(self._findings))):
                local = json.loads(json.dumps(document))
                local["finding_id"] = _SENTINEL
                local["organization_id"] = str(organization_id)
                local["plant_id"] = str(plant_id)
                local["zone_id"] = str(zone.zone_id)
                local["node_id"] = str(zone.node_id)
                for camera in local["cameras"]:
                    for clip in camera["clips"]:
                        clip["clip_id"] = str(_uuid7(_CLIP_EPOCH, self._rng))
                        clip["storage_key"] = (
                            f"org/{organization_id}/plant/{plant_id}/zone/{zone.zone_id}"
                            f"/node/{zone.node_id}/{clip['clip_id']}.mp4"
                        )
                templates.append(canonicalize(local))
            self._templates[zone.zone_id] = templates
        return templates

    def finding(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone: ZoneRef,
        finding_id: uuid.UUID,
    ) -> bytes:
        template = self._rng.choice(self._zone_templates(organization_id, plant_id, zone))
        return template.replace(_SENTINEL.encode(), str(finding_id).encode())

    @staticmethod
    def observability(
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone: ZoneRef,
        *,
        event_id: uuid.UUID,
        zone_subject: bool,
        started_at: datetime,
        ended_at: datetime | None,
        opened_event_id: uuid.UUID | None,
    ) -> bytes:
        subject: dict[str, Any] = {"kind": "zone"}
        if not zone_subject:
            subject = {"kind": "camera", "camera_id": str(zone.camera_id)}
        document: dict[str, Any] = {
            "event_id": str(event_id),
            "contract_version": "1.0.0",
            "organization_id": str(organization_id),
            "plant_id": str(plant_id),
            "zone_id": str(zone.zone_id),
            "node_id": str(zone.node_id),
            "subject": subject,
            "phase": "opened" if opened_event_id is None else "closed",
            "state": "degraded",
            "causes": ["backlight"],
            "started_at": stamp(started_at),
            "node_time": {
                "started_at": stamp(started_at),
                "ended_at": stamp(ended_at or started_at),
                "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp.local"},
            },
            "evidence": [],
            "software_version": "1.0.0",
        }
        if opened_event_id is not None and ended_at is not None:
            document["ended_at"] = stamp(ended_at)
            document["opened_event_id"] = str(opened_event_id)
        return canonicalize(document)

    def classification(
        self, plant_id: uuid.UUID, zone: ZoneRef, classification_id: uuid.UUID
    ) -> bytes:
        return canonicalize(
            {
                "classification_id": str(classification_id),
                "plant_id": str(plant_id),
                "zone_id": str(zone.zone_id),
                "anchor_record_id": str(_uuid(self._rng)),
                "family": self._rng.choice(["dwell", "coexistence", "guard_bypass"]),
                "outcome": self._rng.choice(["confirmed", "authorized_operation"]),
                "reason_category": "guard_open",
                "signer": {"user_id": str(_uuid(self._rng)), "role": "coordinator_sst"},
            }
        )


# --- Escritura --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    received_at: datetime
    record_type: str
    plant_id: uuid.UUID
    zone_id: uuid.UUID | None
    node_id: uuid.UUID | None
    content: bytes
    source_key: str | None = None
    occurred_at: datetime | None = None
    actor_kind: str = "system"
    actor_unit: str = "U-03"
    actor_role: str | None = None


_INSERT_RECORDS: Final = (
    "INSERT INTO ledger.ledger_record (record_id, organization_id, plant_id, record_type,"
    " schema_version, actor_kind, actor_id, actor_display_name_snapshot, actor_role_in_use,"
    " actor_unit, scope_plant_id, scope_zone_id, scope_node_id, correlation_id, received_at,"
    " occurred_at, source_key, content, chain_sequence, content_hash, previous_hash,"
    " record_hash)"
    " SELECT r.record_id, $1, r.plant_id, r.record_type, 1, r.actor_kind, r.actor_id,"
    " CASE WHEN r.actor_kind = 'user' THEN 'Coordinación SST sintética'"
    " ELSE 'Nodo sintético' END, r.actor_role, r.actor_unit, r.plant_id, r.zone_id, r.node_id,"
    " r.correlation_id, r.received_at, r.occurred_at, r.source_key, r.content, 1, $2, $2, $2"
    " FROM unnest($3::uuid[], $4::uuid[], $5::text[], $6::text[], $7::uuid[], $8::text[],"
    " $9::text[], $10::uuid[], $11::uuid[], $12::uuid[], $13::timestamptz[],"
    " $14::timestamptz[], $15::text[], $16::bytea[])"
    " AS r(record_id, plant_id, record_type, actor_kind, actor_id, actor_role, actor_unit,"
    " zone_id, node_id, correlation_id, received_at, occurred_at, source_key, content)"
    " ORDER BY r.received_at"
)

_INSERT_AUDIT: Final = (
    "INSERT INTO shared.audit_entry (entry_id, organization_id, actor_kind, actor_id,"
    " actor_display_name_snapshot, actor_role_in_use, actor_unit, operation, scope_plant_id,"
    " scope_zone_id, filters, result_count, outcome, correlation_id, occurred_at,"
    " chain_sequence, previous_hash, entry_hash)"
    " SELECT a.entry_id, $1, 'user', a.actor_id, 'Coordinación SST sintética',"
    " 'coordinator_sst', 'U-02', a.operation, a.plant_id, a.zone_id, a.filters,"
    " a.result_count, 'success', a.correlation_id, a.occurred_at, 1, $2, $2"
    " FROM unnest($3::uuid[], $4::uuid[], $5::text[], $6::uuid[], $7::uuid[], $8::bytea[],"
    " $9::int[], $10::uuid[], $11::timestamptz[])"
    " AS a(entry_id, actor_id, operation, plant_id, zone_id, filters, result_count,"
    " correlation_id, occurred_at)"
    " ORDER BY a.occurred_at"
)

_PLACEHOLDER_HASH: Final = "0" * 64


@dataclass
class ScaleWriter:
    """Escribe la historia de plantas y de la auditoría con el disparador real.

    Cada llamada usa su propia conexión de superusuario dentro de ``historical_stamps`` y escribe
    en orden cronológico dentro de cada cadena (la marca nunca retrocede).
    """

    findings: Sequence[dict[str, Any]]
    seed: int
    rates: Rates = field(default_factory=Rates)
    written: Counter[str] = field(default_factory=Counter)

    def _rng(self, *parts: object) -> random.Random:
        material = ":".join(str(part) for part in (self.seed, *parts)).encode()
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))  # noqa: S311 - datos sintéticos

    async def bootstrap_plant(
        self, connection: Any, organization: OrganizationRef, plant: PlantRef, at: datetime
    ) -> None:
        """Compuerta productiva de cada zona y nodo ``reachable`` (con su proyección) en ``at``."""
        rng = self._rng("bootstrap", plant.plant_id)
        rows: list[_Row] = []
        for zone in plant.zones:
            rows.append(
                _Row(
                    received_at=at,
                    record_type=GATE_TYPE,
                    plant_id=plant.plant_id,
                    zone_id=zone.zone_id,
                    node_id=None,
                    content=canonicalize(
                        {
                            "zone_id": str(zone.zone_id),
                            "plant_id": str(plant.plant_id),
                            "gate": "use",
                            "status": "approved",
                            "resulting_mode": "productive",
                        }
                    ),
                    occurred_at=at,
                )
            )
            rows.append(
                _Row(
                    received_at=at,
                    record_type=COMMUNICATION_TYPE,
                    plant_id=plant.plant_id,
                    zone_id=None,
                    node_id=zone.node_id,
                    content=canonicalize(
                        {
                            "node_id": str(zone.node_id),
                            "plant_id": str(plant.plant_id),
                            "state": "reachable",
                            "since": stamp(at),
                        }
                    ),
                )
            )
        identifiers = await self._insert(connection, organization, rows, rng)
        async with connection.transaction():
            for row, record_id in zip(rows, identifiers, strict=True):
                if row.record_type != COMMUNICATION_TYPE:
                    continue
                await connection.execute(
                    "INSERT INTO ledger.communication_state (node_id, organization_id, plant_id,"
                    " state, since, last_heartbeat_at, source_record_id)"
                    " VALUES ($1, $2, $3, 'reachable', $4, NULL, $5)",
                    row.node_id,
                    organization.organization_id,
                    plant.plant_id,
                    at,
                    record_id,
                )

    def _plant_day(
        self,
        organization: OrganizationRef,
        plant: PlantRef,
        day: datetime,
        contents: _Contents,
        hot_zone: uuid.UUID | None,
    ) -> list[_Row]:
        rng = self._rng("day", plant.plant_id, day.isoformat())
        seconds = DAY.total_seconds()
        rows: list[_Row] = []
        for zone in plant.zones:
            for _ in range(self.rates.findings):
                at = day + timedelta(seconds=rng.uniform(0, seconds))
                finding_id = _uuid7(at, rng)
                rows.append(
                    _Row(
                        received_at=at,
                        record_type=FINDING_TYPE,
                        plant_id=plant.plant_id,
                        zone_id=zone.zone_id,
                        node_id=zone.node_id,
                        content=contents.finding(
                            organization.organization_id, plant.plant_id, zone, finding_id
                        ),
                        source_key=str(finding_id),
                        occurred_at=at - timedelta(seconds=20),
                    )
                )
            for _ in range(self.rates.classifications):
                at = day + timedelta(seconds=rng.uniform(0, seconds))
                classification_id = _uuid(rng)
                rows.append(
                    _Row(
                        received_at=at,
                        record_type=CLASSIFICATION_TYPE,
                        plant_id=plant.plant_id,
                        zone_id=zone.zone_id,
                        node_id=None,
                        content=contents.classification(plant.plant_id, zone, classification_id),
                        source_key=str(classification_id),
                        actor_kind="user",
                        actor_unit="U-04",
                        actor_role="coordinator_sst",
                    )
                )
            pairs = (
                self.rates.hot_zone_pairs
                if zone.zone_id == hot_zone
                else self.rates.observability_pairs
            )
            spacing = seconds / max(pairs, 1)
            for index in range(pairs):
                opened_at = day + timedelta(seconds=index * spacing + rng.uniform(0, spacing / 4))
                closed_at = opened_at + timedelta(seconds=rng.uniform(0.2, 0.7) * spacing)
                opened_at = opened_at.replace(microsecond=opened_at.microsecond // 1000 * 1000)
                closed_at = closed_at.replace(microsecond=closed_at.microsecond // 1000 * 1000)
                zone_subject = index % 2 == 0
                opened_id = _uuid7(opened_at, rng)
                for event_id, received, ended, opener in (
                    (opened_id, opened_at, None, None),
                    (_uuid7(closed_at, rng), closed_at, closed_at, opened_id),
                ):
                    rows.append(
                        _Row(
                            received_at=received + timedelta(milliseconds=150),
                            record_type=OBSERVABILITY_TYPE,
                            plant_id=plant.plant_id,
                            zone_id=zone.zone_id,
                            node_id=zone.node_id,
                            content=contents.observability(
                                organization.organization_id,
                                plant.plant_id,
                                zone,
                                event_id=event_id,
                                zone_subject=zone_subject,
                                started_at=opened_at,
                                ended_at=ended,
                                opened_event_id=opener,
                            ),
                            source_key=str(event_id),
                            occurred_at=received,
                        )
                    )
            for _ in range(self.rates.communication_pairs):
                # El nodo calla (mute desde su último latido) y vuelve unos minutos después.
                muted = day + timedelta(seconds=rng.uniform(0, seconds - 3600))
                muted = muted.replace(microsecond=muted.microsecond // 1000 * 1000)
                back = muted + timedelta(seconds=rng.uniform(300, 1800))
                for at, state, extra in (
                    (muted, "mute", {"last_heartbeat_at": stamp(muted - timedelta(minutes=5))}),
                    (back, "reachable", {}),
                ):
                    rows.append(
                        _Row(
                            received_at=at,
                            record_type=COMMUNICATION_TYPE,
                            plant_id=plant.plant_id,
                            zone_id=None,
                            node_id=zone.node_id,
                            content=canonicalize(
                                {
                                    "node_id": str(zone.node_id),
                                    "plant_id": str(plant.plant_id),
                                    "state": state,
                                    "since": stamp(at),
                                    **extra,
                                }
                            ),
                        )
                    )
        rows.sort(key=lambda row: row.received_at)
        return rows

    async def plant_history(
        self,
        connection: Any,
        organization: OrganizationRef,
        plant: PlantRef,
        first_day: datetime,
        days: int,
        *,
        hot_zone: uuid.UUID | None = None,
        hot_days: int | None = None,
    ) -> None:
        """``days`` días de la planta desde ``first_day`` (a medianoche UTC), día a día.

        ``hot_zone`` lleva el volumen máximo de eventos (``Rates.hot_zone_pairs``) en sus últimos
        ``hot_days`` días (todos si es ``None``) y el normal antes.
        """
        contents = _Contents(self.findings, self._rng("contents", plant.plant_id))
        rng = self._rng("ids", plant.plant_id)
        hot_from = 0 if hot_days is None else days - hot_days
        for offset in range(days):
            rows = self._plant_day(
                organization,
                plant,
                first_day + offset * DAY,
                contents,
                hot_zone if offset >= hot_from else None,
            )
            await self._insert(connection, organization, rows, rng)

    async def _insert(
        self,
        connection: Any,
        organization: OrganizationRef,
        rows: Sequence[_Row],
        rng: random.Random,
    ) -> list[uuid.UUID]:
        identifiers = [_uuid7(row.received_at, rng) for row in rows]
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true)",
                str(organization.organization_id),
            )
            await connection.execute(
                _INSERT_RECORDS,
                organization.organization_id,
                _PLACEHOLDER_HASH,
                identifiers,
                [row.plant_id for row in rows],
                [row.record_type for row in rows],
                [row.actor_kind for row in rows],
                [_uuid(rng) for _ in rows],
                [row.actor_role for row in rows],
                [row.actor_unit for row in rows],
                [row.zone_id for row in rows],
                [row.node_id for row in rows],
                [_uuid(rng) for _ in rows],
                [row.received_at for row in rows],
                [row.occurred_at for row in rows],
                [row.source_key for row in rows],
                [row.content for row in rows],
            )
        self.written.update(row.record_type for row in rows)
        return identifiers

    async def audit_history(
        self,
        connection: Any,
        organization: OrganizationRef,
        first_day: datetime,
        days: int,
        per_day: int,
    ) -> None:
        """``per_day`` entradas de auditoría al día (lecturas de lista y de cobertura)."""
        rng = self._rng("audit", organization.organization_id)
        zones = [
            (plant.plant_id, zone.zone_id) for plant in organization.plants for zone in plant.zones
        ]
        for offset in range(days):
            day = first_day + offset * DAY
            moments = sorted(
                day + timedelta(seconds=rng.uniform(0, DAY.total_seconds())) for _ in range(per_day)
            )
            entries: list[tuple[uuid.UUID, str, uuid.UUID, uuid.UUID, bytes, int, datetime]] = []
            for moment in moments:
                plant_id, zone_id = rng.choice(zones)
                if rng.random() < 0.7:
                    operation = "ledger_read"
                    filters: dict[str, JsonValue] = {"zone_id": str(zone_id), "page_size": 200}
                    count = rng.randint(0, 200)
                else:
                    operation = "coverage_read"
                    filters = {
                        "zone_id": str(zone_id),
                        "period_from": stamp(moment - timedelta(days=31)),
                        "period_to": stamp(moment),
                    }
                    count = rng.randint(1, 400)
                entries.append(
                    (_uuid(rng), operation, plant_id, zone_id, canonicalize(filters), count, moment)
                )
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true)",
                    str(organization.organization_id),
                )
                await connection.execute(
                    _INSERT_AUDIT,
                    organization.organization_id,
                    _PLACEHOLDER_HASH,
                    [entry[0] for entry in entries],
                    [_uuid(rng) for _ in entries],
                    [entry[1] for entry in entries],
                    [entry[2] for entry in entries],
                    [entry[3] for entry in entries],
                    [entry[4] for entry in entries],
                    [entry[5] for entry in entries],
                    [_uuid(rng) for _ in entries],
                    [entry[6] for entry in entries],
                )
            self.written["audit_entry"] += len(entries)


def days_back(end: datetime, days: int) -> datetime:
    """Medianoche UTC ``days`` días antes del día de ``end``."""
    midnight = end.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - days * DAY


def chunked[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- U-03: gobernanza y flota (TASK-233; NFR-GOB-07, 11 y 16) ------------------------------------
#
# Lo que la volumetría de U-03 escribe **en masa** como superusuario, con la forma que dejan las
# rutas reales (las filas de inventario y de catálogo se copian de un nodo y una zona creados por
# las rutas: ``generate_scale_data.py``). Las tablas grandes se llenan con ``generate_series`` en
# el servidor, una sentencia por nodo o por zona: 13 millones de latidos no pasan por Python.


@dataclass(frozen=True, slots=True)
class GobRates:
    """Volumen de U-03 por nodo y zona ``[estimación propia]`` (NFR-GOB-07 y 16)."""

    heartbeat_seconds: int = 60
    """Un latido por minuto: 144 000 al día con 100 nodos."""
    heartbeat_days: int = 90
    """``HeartbeatHistory`` en línea 90 días (BR-GOB-72)."""
    grants_per_zone_day: int = 77
    """Una concesión de subida por episodio (77 por zona y día, RNF-DES-03)."""
    grant_days: int = 30
    history_months: int = 24
    """Catálogo, compuertas, intentos de alta y alarmas: 24 meses en línea (NFR-GOB-16, 39)."""
    catalog_versions_per_month: int = 4
    """Una versión del catálogo por semana."""
    attempts_per_node_month: int = 2
    """Un alta aceptada y un intento rechazado al mes por nodo (rotaciones y errores)."""
    alarms_per_node_month: int = 4


@dataclass(frozen=True, slots=True)
class FleetNode:
    """Un nodo de la flota generada con su zona y sus cámaras."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    cameras: tuple[uuid.UUID, ...]


async def create_fleet_partitions(connection: Any, first: datetime, last: datetime) -> None:
    """Particiones mensuales de ``heartbeat_history``, ``enrollment_attempt`` y ``fleet_alarm``
    de ``first`` a ``last`` con la función de gob_0018 (actor system)."""
    async with connection.transaction():
        await connection.execute("SELECT set_config('vigia.actor_kind', 'system', true)")
        rows = await connection.fetch(
            "SELECT * FROM shared.vigia_create_fleet_month_partitions($1, $2)",
            month_floor(first).date(),
            month_floor(last).date(),
        )
    blocked = [row["partition"] for row in rows if row["blocked"]]
    if blocked:
        raise RuntimeError(f"particiones de flota bloqueadas: {blocked}")


async def enroll_fleet(
    connection: Any, nodes: Sequence[FleetNode], user_id: uuid.UUID, at: datetime
) -> None:
    """``NodeFleetRecord`` y ``NodeCredential`` activos de ``nodes`` dados de alta en ``at``."""
    await connection.executemany(
        "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id,"
        " hardware_fingerprint, declared_at, declared_by, enrolled_at)"
        " VALUES ($1::uuid, $2, $3, encode(sha256($1::uuid::text::bytea), 'hex'), $4, $5, $4)",
        [(n.node_id, n.organization_id, n.plant_id, at, user_id) for n in nodes],
    )
    await connection.executemany(
        "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
        " certificate_serial, subject, issued_at, expires_at)"
        " VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,"
        " replace($3::uuid::text, '-', ''),"
        " jsonb_build_object('node_id', $3::uuid::text, 'organization_id', $1::uuid::text,"
        " 'plant_id', $2::uuid::text), $4::timestamptz, $4::timestamptz + interval '365 days')",
        [(n.organization_id, n.plant_id, n.node_id, at) for n in nodes],
    )


async def copy_inventory(
    connection: Any, template_node: uuid.UUID, nodes: Sequence[FleetNode], at: datetime
) -> None:
    """``NodeInventory``, ``CameraInventory`` y ``ZoneNodeState`` de ``nodes`` con la forma que
    dejó el latido real de ``template_node`` (último latido en ``at``)."""
    template = await connection.fetchrow(
        "SELECT * FROM fleet.node_inventory WHERE node_id = $1", template_node
    )
    camera = await connection.fetchrow(
        "SELECT * FROM fleet.camera_inventory WHERE node_id = $1 LIMIT 1", template_node
    )
    zone = await connection.fetchrow(
        "SELECT * FROM fleet.zone_node_state WHERE node_id = $1 LIMIT 1", template_node
    )
    if template is None or camera is None or zone is None:
        raise RuntimeError("el nodo plantilla no tiene inventario: falta su latido")
    await connection.executemany(
        "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id, software_version,"
        " contract_version, model_version, contract_notice, last_heartbeat_at,"
        " communication_state, local_queue, clock, signal_reader, uptime_seconds, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'reachable', $9, $10, $11, $12, $8)",
        [
            (n.node_id, n.organization_id, n.plant_id, template["software_version"],
             template["contract_version"], template["model_version"],
             template["contract_notice"], at, template["local_queue"], template["clock"],
             template["signal_reader"], template["uptime_seconds"])
            for n in nodes
        ],
    )  # fmt: skip
    await connection.executemany(
        "INSERT INTO fleet.camera_inventory (organization_id, plant_id, node_id, camera_id,"
        " connected, measured_fps, declared_min_fps, observability_state, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        [
            (n.organization_id, n.plant_id, n.node_id, camera_id, camera["connected"],
             camera["measured_fps"], camera["declared_min_fps"], camera["observability_state"],
             at)
            for n in nodes
            for camera_id in n.cameras
        ],
    )  # fmt: skip
    await connection.executemany(
        "INSERT INTO fleet.zone_node_state (organization_id, plant_id, node_id, zone_id, mode,"
        " observability_state, catalog_version_in_node, gate_state_valid_until, open_episodes,"
        " coverage_ok, updated_at) VALUES ($1, $2, $3, $4, $5, $6, 1, $7, 0, $8, $9)",
        [
            (n.organization_id, n.plant_id, n.node_id, n.zone_id, zone["mode"],
             zone["observability_state"], at + timedelta(days=7), zone["coverage_ok"], at)
            for n in nodes
        ],
    )  # fmt: skip


async def heartbeat_history(
    connection: Any,
    node: FleetNode,
    first: datetime,
    last: datetime,
    summary: Mapping[str, Any],
    *,
    every: int,
) -> int:
    """Un latido cada ``every`` segundos de ``first`` a ``last`` (exclusivo) con ``summary``."""
    status: str = await connection.execute(
        "INSERT INTO fleet.heartbeat_history (heartbeat_id, organization_id, plant_id, node_id,"
        " received_at, sent_at, payload_summary)"
        " SELECT gen_random_uuid(), $1, $2, $3, t, t - interval '150 milliseconds', $4::jsonb"
        " FROM generate_series($5::timestamptz, $6::timestamptz - interval '1 millisecond',"
        " make_interval(secs => $7)) AS t",
        node.organization_id,
        node.plant_id,
        node.node_id,
        json.dumps(summary),
        first,
        last,
        every,
    )
    return int(status.rsplit(" ", 1)[-1])


async def upload_grants(
    connection: Any, node: FleetNode, first_day: datetime, days: int, per_day: int
) -> int:
    """``per_day`` concesiones de subida al día de la zona de ``node`` durante ``days`` días,
    usadas a los 30 s (las que la ingesta cita); una de cada 50, huérfana al día siguiente."""
    status: str = await connection.execute(
        "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
        " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
        " issued_at, expires_at, status, used_at, orphaned_at)"
        " SELECT g.clip_id, $1::uuid, $2::uuid, $3::uuid, $4::uuid, 'evidence', 'org/'"
        " || $1::uuid::text || '/plant/'"
        " || $2::uuid::text || '/zone/' || $3::uuid::text || '/node/' || $4::uuid::text"
        " || '/' || g.clip_id::text"
        " || '.mp4', 'video/mp4', 52428800, jsonb_build_object('content-type', 'video/mp4',"
        " 'x-amz-checksum-sha256', 'X5uM2q0pT7v3sQ9wZxY1bC4dE6fG8hJ0kL2mN4pR6tU=',"
        " 'x-amz-meta-vigia-anonymized', '1'), g.issued_at, g.issued_at + interval '15 minutes',"
        " CASE WHEN g.n % 50 = 0 THEN 'orphan' ELSE 'used' END,"
        " g.issued_at + interval '30 seconds',"
        " CASE WHEN g.n % 50 = 0 THEN g.issued_at + interval '25 hours' END"
        " FROM (SELECT gen_random_uuid() AS clip_id, n,"
        " $5::timestamptz + (n * interval '1 day') / $7::int AS issued_at"
        " FROM generate_series(0, $6::int * $7::int - 1) AS n) AS g",
        node.organization_id,
        node.plant_id,
        node.zone_id,
        node.node_id,
        first_day,
        days,
        per_day,
    )
    return int(status.rsplit(" ", 1)[-1])


async def enrollment_attempts(
    connection: Any, node: FleetNode, first: datetime, months: int, per_month: int
) -> int:
    """``per_month`` intentos de alta al mes durante ``months`` meses: uno aceptado y el resto
    rechazados por código usado (forma de ``EnrollmentAttempt``, NFR-GOB-16: 24 meses)."""
    status: str = await connection.execute(
        "INSERT INTO fleet.enrollment_attempt (attempt_id, organization_id, plant_id, node_id,"
        " presented_code_hash, hardware_fingerprint, software_version, contract_version, result,"
        " attempted_at, source_ip_hash, correlation_id, ledger_record_id)"
        " SELECT gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,"
        " encode(sha256(n::text::bytea), 'hex'),"
        " encode(sha256($3::uuid::text::bytea), 'hex'), '1.4.0', '1.0.0',"
        " CASE WHEN n % $5::int = 0 THEN 'accepted' ELSE 'enrollment_code_used' END,"
        " $4::timestamptz + n * (interval '30 days' / $5::int),"
        " encode(sha256(('origen' || n)::bytea), 'hex'), gen_random_uuid(),"
        " CASE WHEN n % $5::int = 0 THEN gen_random_uuid() END"
        " FROM generate_series(0, $6::int * $5::int - 1) AS n",
        node.organization_id,
        node.plant_id,
        node.node_id,
        first,
        per_month,
        months,
    )
    return int(status.rsplit(" ", 1)[-1])


async def fleet_alarms(
    connection: Any, node: FleetNode, first: datetime, months: int, per_month: int
) -> int:
    """``per_month`` alarmas de flota cerradas al mes durante ``months`` meses (clases de
    NFR-GOB-46 rotando; cada una abierta 40 minutos)."""
    status: str = await connection.execute(
        "INSERT INTO fleet.fleet_alarm (alarm_id, organization_id, plant_id, alarm_kind,"
        " node_id, zone_id, raised_at, cleared_at, raised_event_id, cleared_event_id)"
        " SELECT gen_random_uuid(), $1, $2, (ARRAY['node_mute', 'queue_over_threshold',"
        " 'clock_drift', 'camera_below_min_fps'])[1 + n % 4], $3, NULL,"
        " $4::timestamptz + n * (interval '30 days' / $5::int),"
        " $4::timestamptz + n * (interval '30 days' / $5::int) + interval '40 minutes',"
        " gen_random_uuid(), gen_random_uuid()"
        " FROM generate_series(0, $6::int * $5::int - 1) AS n",
        node.organization_id,
        node.plant_id,
        node.node_id,
        first,
        per_month,
        months,
    )
    return int(status.rsplit(" ", 1)[-1])


def lean_findings(count: int) -> list[dict[str, Any]]:
    """Hallazgos mínimos (sin cámaras ni clips) para la historia del expediente de U-03: lo que
    recorren sus lecturas es el número de registros, no su contenido."""
    return [
        {
            "finding_id": _SENTINEL,
            "family": "coexistence",
            "tier": "tier_1",
            "max_confidence": round(0.5 + index / (2 * count), 3),
            "cameras": [],
        }
        for index in range(count)
    ]


# --- Historia del catálogo y de las compuertas (U-03; también el banco de NFR-GOB-04) ----------

CATALOG_REASON: Final = "Motivo sintético del cambio del catálogo"
_MONTH: Final = timedelta(days=30)


def catalog_payload(
    zone: uuid.UUID,
    version: int,
    standards: Sequence[uuid.UUID],
    cameras: Sequence[uuid.UUID],
) -> str:
    """Un ``ZoneCatalog`` del tamaño real: sus estándares, sus cámaras, señales y parámetros."""
    payload = {
        "version": version,
        "zone_id": str(zone),
        "cameras": [
            {"camera_id": str(camera), "code": f"CAM-{i}"} for i, camera in enumerate(cameras)
        ],
        "minimum_coverage": {"required_count": 1, "required_camera_ids": []},
        "signals": [{"signal_id": f"s{i}", "role": "energy"} for i in range(4)],
        "thresholds": {"review": 0.5, "publication": 0.8},
        "clip_window": {"pre_ms": 5000, "post_ms": 5000},
        "episode": {"grouping_window_ms": 3000},
        "standards": [
            {
                "standard_id": str(standard),
                "version": 1 + version // len(standards),
                "family": "coexistence",
                "title_es": f"Estándar sintético {index}",
                "declared_text": "Texto declarado sintético del estándar de la zona. " * 4,
                "predicate": {"all_of": [{"signal": "presence"}, {"signal": "energy"}]},
            }
            for index, standard in enumerate(standards)
        ],
    }  # fmt: skip
    return json.dumps(payload)


@dataclass(frozen=True)
class CatalogZone:
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standards: tuple[uuid.UUID, ...]
    cameras: tuple[uuid.UUID, ...]


@dataclass
class CatalogHistory:
    """Las filas de ``catalog`` de una organización (ver ``catalog_history``)."""

    zones: list[CatalogZone] = field(default_factory=list)
    versions: list[tuple[Any, ...]] = field(default_factory=list)
    standards: list[tuple[Any, ...]] = field(default_factory=list)
    intervals: list[tuple[Any, ...]] = field(default_factory=list)
    projections: list[tuple[Any, ...]] = field(default_factory=list)
    regressions: list[tuple[Any, ...]] = field(default_factory=list)
    agreements: list[tuple[Any, ...]] = field(default_factory=list)
    confirmations: list[tuple[Any, ...]] = field(default_factory=list)
    policies: list[tuple[Any, ...]] = field(default_factory=list)


def catalog_history(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    signers: Sequence[tuple[str, uuid.UUID]],
    zones: Sequence[CatalogZone],
    plants: Sequence[uuid.UUID],
    *,
    origin: datetime,
    now: datetime,
    rng: random.Random,
    versions: int,
    version_spacing: timedelta,
    gate_intervals: int,
    agreements: int,
    policies: int,
) -> CatalogHistory:
    """La historia del catálogo y de las compuertas de ``zones`` desde ``origin``.

    Por zona: ``versions`` versiones del catálogo (una cada ``version_spacing``, con su
    ``ZoneCatalog`` y su sobre), cada versión nueva reversiona un estándar; ``gate_intervals``
    intervalos mensuales por compuerta (aprobación y revocación alternas, el último abierto);
    ``agreements`` acuerdos de uso mensuales con la confirmación de cada firmante (el último
    vigente), su proyección y su regresión. Por planta, ``policies`` versiones de la política.
    """
    history = CatalogHistory(zones=list(zones))
    signatories = json.dumps(
        [{"role": r, "user_id": str(u), "display_name": f"Firmante {r}"} for r, u in signers]
    )
    document = json.dumps({"document_id": str(uuid.uuid4()), "storage_key": "documents/x",
                           "sha256": "a" * 64, "content_type": "application/pdf",
                           "size_bytes": 1024})  # fmt: skip
    for zone in zones:
        plant, ids = zone.plant_id, zone.standards
        current = dict.fromkeys(ids, 1)
        spans: dict[tuple[uuid.UUID, int], list[Any]] = {}
        for number in range(1, versions + 1):
            issued = origin + (number - 1) * version_spacing
            until = None if number == versions else issued + version_spacing
            payload = catalog_payload(zone.zone_id, number, ids, zone.cameras)
            envelope = json.dumps({"payload": json.loads(payload), "signature": "s" * 88,
                                   "key_id": "k1"})  # fmt: skip
            history.versions.append(
                (organization_id, plant, zone.zone_id, number, issued, user_id,
                 f"{CATALOG_REASON} {number}", ["standards"], payload, envelope,
                 number % 5 == 0, 15 + number % 400, uuid.uuid4(), until)
            )  # fmt: skip
            if number == 1:
                for s in ids:
                    spans[s, 1] = [organization_id, plant, zone.zone_id, s, 1, issued, 1, None]
                continue
            # Cada versión nueva del catálogo reversiona un estándar y retira la anterior.
            standard = ids[(number - 2) % len(ids)]
            previous = current[standard]
            spans[standard, previous][7] = number
            spans[standard, previous + 1] = [
                organization_id, plant, zone.zone_id, standard, previous + 1, issued, number, None
            ]  # fmt: skip
            current[standard] = previous + 1
        history.standards.extend(tuple(row) for row in spans.values())
        for gate in ("mounting", "usage"):
            start = origin + timedelta(hours=rng.randrange(1, 48))
            for index in range(gate_intervals):
                status = "approved" if index % 2 == 0 else "revoked"
                end = None if index == gate_intervals - 1 else start + _MONTH
                history.intervals.append(
                    (organization_id, plant, zone.zone_id, gate, status, start, end, user_id,
                     CATALOG_REASON if status == "revoked" else None, uuid.uuid4(),
                     uuid.uuid4())
                )  # fmt: skip
                if end is not None:
                    start = end
        decided = json.dumps(
            {
                "status": "revoked",
                "decided_at": now.isoformat(),
                "record_id": str(uuid.uuid4()),
                "decided_by": str(user_id),
            }
        )
        history.projections.append(
            (zone.zone_id, organization_id, plant, decided, decided, "no_capture", now)
        )
        history.regressions.append(
            (
                zone.zone_id,
                organization_id,
                plant,
                now - _MONTH,
                "catalog_change",
                uuid.uuid4(),
                versions,
            )
        )
        previous_agreement: uuid.UUID | None = None
        for index in range(agreements):
            agreement = uuid.uuid4()
            approved = origin + index * _MONTH + timedelta(days=1)
            last = index == agreements - 1
            superseded = None if last else approved + _MONTH
            history.agreements.append(
                (agreement, organization_id, plant, zone.zone_id,
                 "approved" if last else "superseded", signatories, document,
                 previous_agreement, user_id, approved, approved, user_id, uuid.uuid4(),
                 superseded)
            )  # fmt: skip
            confirmed = approved - timedelta(hours=1)
            history.confirmations.extend(
                (agreement, u, organization_id, plant, r, confirmed) for r, u in signers
            )
            previous_agreement = agreement
    for plant in plants:
        for version in range(1, policies + 1):
            history.policies.append(
                (uuid.uuid4(), organization_id, plant, version, origin + version * _MONTH,
                 document, user_id, uuid.uuid4())
            )  # fmt: skip
    return history


async def load_catalog_history(admin: Any, history: CatalogHistory) -> None:
    """Escribe ``history`` como superusuario y deja las estadísticas al día (``ANALYZE``)."""
    await admin.executemany(
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
        " payload, envelope, single_occupancy, aggregation_window_minutes, ledger_record_id,"
        " superseded_at) VALUES ($1, $2, $3, $4, $5, $6, 'administrator', $7, $8, $9, $10,"
        " $11, $12, $13, $14)",
        history.versions,
    )
    await admin.executemany(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, retired_in_catalog_version, reason_es)"
        " VALUES ($1, $2, $3, $4, $5, 'coexistence', 'Estándar sintético',"
        ' \'Texto declarado sintético\', \'{"user_id": "00000000-0000-4000-8000-000000000001",'
        ' "display_name": "Firmante", "role": "administrator"}\', $6, \'{}\', $7, $8,'
        " 'Motivo sintético del estándar')",
        history.standards,
    )
    await admin.executemany(
        "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
        " status, effective_from, effective_until, decided_by, reason_es, ledger_record_id,"
        " record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        history.intervals,
    )
    await admin.executemany(
        "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
        " usage, resulting_mode, issued_at, envelope, valid_until)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, '{}', $7::timestamptz + interval '7 days')",
        history.projections,
    )
    await admin.executemany(
        "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
        " marked_at, cause, catalog_version, affected_row_ids, ledger_record_id)"
        " VALUES ($1, $2, $3, 'pending', $4, $5, $7, '\"all\"', $6)",
        history.regressions,
    )
    await admin.executemany(
        "INSERT INTO catalog.use_agreement (agreement_id, organization_id, plant_id, zone_id,"
        " status, signatories, document_ref, replaces_agreement_id, created_by, created_at,"
        " approved_at, approved_by, ledger_record_id, superseded_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
        history.agreements,
    )
    await admin.executemany(
        "INSERT INTO catalog.agreement_confirmation (agreement_id, user_id, organization_id,"
        " plant_id, role_in_use, confirmed_at, origin)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'management')",
        history.confirmations,
    )
    await admin.executemany(
        "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
        " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
        " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, 'Firmante sintético', 'REF-SINTETICA', $6,"
        " 'Resumen sintético de criterios', $7, $5, $8)",
        history.policies,
    )
    await admin.execute("ANALYZE")
