"""Volumetría de U-04 a escala objetivo (TASK-353; NFR-LAZ-06 y 09; PAT-LAZ-ESC-04; A-65).

``run_laz(endpoint, profile)`` levanta su propia base migrada (``authz_environment``, el mismo
montaje que las pruebas de U-04 con sesiones reales) y, como superusuario:

**1. Un año de la organización mayor, generado en flujo.** ``profile.zones`` zonas (una por
planta, cada una con su nodo) y ``profile.days`` días hasta ayer, al ritmo de ``LazRates`` (77
episodios por zona y día, RNF-DES-03; las demás tablas en la proporción de la tabla de
NFR-LAZ-06). Nada se arma en Python fila a fila: por tabla se inserta **una fila plantilla por
zona** con los constructores válidos de ``tests/loop_db.py``, ``tests/notifications_db.py`` y
``tests/accreditation_db.py`` (un día antes del periodo) y el servidor la **clona** por tramos de
``CHUNK_DAYS`` días con ``INSERT … SELECT … generate_series``: identificadores nuevos
(``gen_random_uuid``) en cada columna de clave, de idempotencia o de referencia a registros, todas
las marcas desplazadas por el mismo intervalo (se conservan las restricciones entre marcas) y el
alcance de la plantilla (organización, planta, zona, nodo, usuario). Las parejas con clave ajena
(aviso → entrega, acción → cierre) se generan juntas: el hijo apunta al padre recién clonado. Las
particiones mensuales salen de las funciones de la base (``vigia_create_loop_month_partitions`` y
``vigia_create_notifications_month_partitions``), las mismas que usa ``create_partitions``.

**2. Métricas de NFR-LAZ-06** sobre lo generado (antes de añadir los avisos de la paginación):
``loop_rows_per_day`` (filas por día de cada tabla, medidas con ``count(*)``) y
``loop_bytes_per_table`` (``pg_total_relation_size`` del árbol de particiones: datos, índices y
TOAST), por zona y extrapoladas a 300 zonas y un año, y la **comparación con la tabla de
NFR-LAZ-06** por grupo (filas y bytes al año frente a lo estimado): tendencia, nunca decide el
resultado (A-65).

**3. Paginación por clave sobre ``profile.notifications`` avisos** (NFR-LAZ-09) de un solo
destinatario, repartidos en el año con marcas repetidas de tres en tres (empates resueltos por
``notification_id``) y la mitad leídos una hora después de crearse: ``GET /notifications`` por la
aplicación real (``create_app`` con la cadena fija de middleware, sesión del destinatario y
``NotificationReader``), página a página con ``after`` hasta el final:

- antes del archivado (ninguno archivado, el listado por defecto): **todos** los avisos, ninguno
  repetido, en orden estrictamente decreciente de ``(created_at, notification_id)``;
- **archivado de lo leído a los 60 días** (``archive_read_notifications``, lo que corre
  ``EmailGroupingTask``): el recuento de filas del destinatario es el mismo antes y después (nada
  se borra) y los archivados son exactamente los leídos hace más de 60 días;
- después, sin ``include_archived`` (lo que ve la bandeja por defecto): exactamente los no
  archivados, sin pérdidas ni repeticiones;
- con ``include_archived``, solo las primeras ``INCLUDE_ARCHIVED_PAGES`` páginas y como tendencia:
  el índice de la lista (``notification_recipient_list``) es parcial (``archived_at IS NULL``), así
  que cada página con lo archivado ordena todos los avisos del destinatario (hallazgo de esta
  medición, en el informe).

Solo datos generados. Una conexión de escritura (regla de recursos compartidos); topes de la
base largos (retro 15): ninguno decide un resultado.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from unittest.mock import Mock

import httpx

from tests.accreditation_db import AccreditationScope
from tests.accreditation_db import adoption_counter as adoption_counter_row
from tests.api_support import World
from tests.authz_support import AuthzEnvironment, authz_environment
from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.load.laz_profiles import (
    NFR_LAZ_06,
    NFR_LAZ_06_TOTAL_BYTES,
    TARGET_ZONES,
    LazRates,
    VolumetryProfile,
)
from tests.loop_db import LoopScope
from tests.loop_db import classification as classification_row
from tests.loop_db import closure_record as closure_record_row
from tests.loop_db import corrective_action as corrective_action_row
from tests.loop_db import episode as episode_row
from tests.loop_db import finding_state as finding_state_row
from tests.loop_db import review_queue_item as review_queue_item_row
from tests.loop_db import review_resolution as review_resolution_row
from tests.loop_db import root_cause_analysis as root_cause_analysis_row
from tests.loop_db import unclosable_declaration as unclosable_declaration_row
from tests.notifications_db import NotificationScope
from tests.notifications_db import digest_batch as digest_batch_row
from tests.notifications_db import email_message as email_message_row
from tests.notifications_db import notification as notification_row
from tests.notifications_db import notification_delivery as notification_delivery_row
from tests.outbox_support import app_database
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
from vigia_platform.notifications.reader import (
    ARCHIVE_AFTER_READ,
    NotificationReader,
    archive_read_notifications,
)
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = ["CHUNK_DAYS", "TABLES", "TableSpec", "run_laz"]

DAY: Final = dt.timedelta(days=1)
WALL: Final = SystemClock()
"""Solo para medir duraciones (informe de tendencia); ninguna decide un resultado."""
CHUNK_DAYS: Final = 30
"""Días por sentencia de clonado: acota el tamaño de cada transacción."""
PAGINATION_CHUNK: Final = 100_000
INCLUDE_ARCHIVED_PAGES: Final = 3
"""Páginas con ``include_archived`` que se miden después del archivado (tendencia)."""
PAGE_PACING_SECONDS: Final = 0.11
"""Avance del reloj simulado por petición: algo menos de 600 por minuto, el límite de la sesión
(``SESSION_BUDGET``, NFR-NUC-25), que la aplicación mide con ese reloj. Sin él, un recorrido de un
millón (5 000 páginas en una sesión) recibe ``rate_limited`` (nightly 39584457311)."""
MAX_RATE_LIMIT_WAITS: Final = 50
"""Esperas de ``retry_after_seconds`` permitidas por recorrido antes de dar el 429 por fallo."""
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
REASON_TEXT: Final = (
    "Confirmado en sitio: la condición de coexistencia observada coincide con el estándar "
    "declarado para la zona y se abre el lazo de corrección."
)
"""Un motivo de longitud típica (las plantillas de ``loop_db`` usan la mínima de 20)."""


@dataclass(frozen=True)
class TableSpec:
    """Cómo se clona una tabla: su plantilla, su ritmo y lo que se regenera en cada fila."""

    table: str
    time_column: str
    """La marca de la plantilla (y clave de partición, si la tabla está particionada)."""
    template: Callable[[_Scopes, dt.datetime], tuple[str, list[Any]]]
    per_day: Callable[[LazRates], int]
    fresh: tuple[str, ...]
    """Columnas con un valor nuevo por fila: claves, idempotencia, referencias a registros."""
    overrides: Mapping[str, str] = field(default_factory=dict)
    child: ChildSpec | None = None


@dataclass(frozen=True)
class ChildSpec:
    """Una tabla hija que apunta por clave ajena a cada fila recién clonada del padre."""

    table: str
    time_column: str
    template: Callable[[_Scopes, dt.datetime], tuple[str, list[Any]]]
    per_day: Callable[[LazRates], int]
    link: str
    """Columna del hijo que lleva el identificador del padre."""
    fresh: tuple[str, ...]


@dataclass(frozen=True)
class _Scopes:
    loop: LoopScope
    notifications: NotificationScope
    accreditation: AccreditationScope


def _rows_of(builder: Callable[..., tuple[str, list[Any]]], kind: str) -> Any:
    def build(scopes: _Scopes, at: dt.datetime) -> tuple[str, list[Any]]:
        return builder(getattr(scopes, kind), at)  # type: ignore[no-any-return]

    return build


def _adoption(scopes: _Scopes, at: dt.datetime) -> tuple[str, list[Any]]:
    return adoption_counter_row(scopes.accreditation, at, day=at.date())


TABLES: Final = (
    TableSpec(
        "loop.episode",
        "started_at",
        _rows_of(episode_row, "loop"),
        lambda rates: rates.episodes,
        ("episode_id", "standard_id", "segment_record_ids", "last_event_id"),
    ),
    TableSpec(
        "loop.finding_state",
        "occurred_at_bucket",
        _rows_of(finding_state_row, "loop"),
        lambda rates: rates.finding_states,
        (
            "finding_state_id",
            "subject_id",
            "anchor_record_id",
            "standard_id",
            "last_event_id",
            "last_record_id",
        ),
    ),
    TableSpec(
        "loop.classification",
        "classified_at",
        _rows_of(classification_row, "loop"),
        lambda rates: rates.classifications,
        ("classification_id", "subject_id", "record_id", "label_id", "correlation_id"),
        {"reason_text": f"'{REASON_TEXT}'"},
    ),
    TableSpec(
        "loop.review_queue_item",
        "received_at",
        _rows_of(review_queue_item_row, "loop"),
        lambda rates: rates.detections,
        ("detection_id", "anchor_record_id", "standard_id", "source_event_id"),
    ),
    TableSpec(
        "loop.review_resolution",
        "resolved_at",
        _rows_of(review_resolution_row, "loop"),
        lambda rates: rates.review_resolutions,
        ("resolution_id", "detection_id", "record_id", "label_id", "correlation_id"),
        {"reason_text": f"'{REASON_TEXT}'"},
    ),
    TableSpec(
        "loop.corrective_action",
        "assigned_at",
        _rows_of(corrective_action_row, "loop"),
        lambda rates: rates.corrective_actions,
        ("action_id", "subject_id", "record_id"),
        child=ChildSpec(
            "loop.closure_record",
            "signed_at",
            _rows_of(closure_record_row, "loop"),
            lambda rates: rates.closure_records,
            "action_id",
            ("closure_id", "subject_id", "record_id", "evidence_entry_ids", "correlation_id"),
        ),
    ),
    TableSpec(
        "loop.unclosable_declaration",
        "declared_at",
        _rows_of(unclosable_declaration_row, "loop"),
        lambda rates: rates.unclosable_declarations,
        ("declaration_id", "subject_id", "record_id", "policy_id"),
    ),
    TableSpec(
        "loop.root_cause_analysis",
        "created_at",
        _rows_of(root_cause_analysis_row, "loop"),
        lambda rates: rates.root_cause_analyses,
        ("analysis_id", "subject_id"),
    ),
    TableSpec(
        "notifications.digest_batch",
        "window_from",
        _rows_of(digest_batch_row, "notifications"),
        lambda rates: rates.digest_batches,
        ("digest_batch_id", "notification_ids"),
    ),
    TableSpec(
        "notifications.notification",
        "created_at",
        _rows_of(notification_row, "notifications"),
        lambda rates: rates.notifications,
        ("notification_id", "source_event_id"),
        child=ChildSpec(
            "notifications.notification_delivery",
            "notification_created_at",
            _rows_of(notification_delivery_row, "notifications"),
            lambda rates: rates.deliveries,
            "notification_id",
            ("delivery_id",),
        ),
    ),
    TableSpec(
        "notifications.email_message",
        "queued_at",
        _rows_of(email_message_row, "notifications"),
        lambda rates: rates.email_messages,
        ("message_id", "related_ids"),
    ),
    TableSpec(
        "accreditation.adoption_counter",
        "last_activity_at",
        _adoption,
        lambda rates: rates.adoption_counters,
        (),
    ),
)
"""Las tablas de NFR-LAZ-06 y las que las acompañan; ``notification_source_key`` la llena el
disparador del aviso."""

MEASURED_TABLES: Final = (
    *(spec.table for spec in TABLES),
    *(spec.child.table for spec in TABLES if spec.child is not None),
    "notifications.notification_source_key",
)


# --- Generación -----------------------------------------------------------------------------


async def _columns(connection: Any, table: str) -> list[tuple[str, str]]:
    schema, name = table.split(".")
    rows = await connection.fetch(
        "SELECT column_name, udt_name FROM information_schema.columns"
        " WHERE table_schema = $1 AND table_name = $2 AND is_generated = 'NEVER'"
        " ORDER BY ordinal_position",
        schema,
        name,
    )
    return [(row["column_name"], row["udt_name"]) for row in rows]


def _expression(
    column: str,
    udt: str,
    fresh: Sequence[str],
    overrides: Mapping[str, str],
    shift: str,
    days: str,
    alias: str = "t",
) -> str:
    if column in overrides:
        return overrides[column]
    if column in fresh:
        if udt == "uuid":
            return "gen_random_uuid()"
        if udt == "_uuid":
            return "ARRAY[gen_random_uuid()]"
        if udt == "_text":
            return "ARRAY[md5(gen_random_uuid()::text) || md5(gen_random_uuid()::text)]"
        return "gen_random_uuid()::text"
    if udt == "timestamptz":
        return f"{alias}.{column} + {shift}"
    if udt == "date":
        return f"{alias}.{column} + {days}"
    return f"{alias}.{column}"


def _shift(per_day: int, slot: str = "g") -> tuple[str, str]:
    """El desplazamiento de la copia ``slot`` (desde la plantilla, un día antes del periodo)."""
    days = f"({slot} / {per_day} + 1)"
    shift = f"({days} * interval '1 day' + ({slot} % {per_day}) * (interval '1 day' / {per_day}))"
    return shift, days


def _template_name(table: str) -> str:
    return "tmpl_" + table.replace(".", "_")


@dataclass
class _Generated:
    templates: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)


class _Generator:
    def __init__(
        self,
        connection: Any,
        organization_id: uuid.UUID,
        profile: VolumetryProfile,
        first_day: dt.datetime,
    ) -> None:
        self._connection = connection
        self._organization = organization_id
        self._profile = profile
        self._first_day = first_day
        self.template_at = first_day - DAY
        self.result = _Generated()

    async def _scope(self) -> None:
        await self._connection.execute(
            "SELECT set_config('vigia.organization_id', $1, true)", str(self._organization)
        )
        await self._connection.execute("SELECT set_config('vigia.actor_kind', 'system', true)")

    async def templates(self, scopes: Sequence[_Scopes]) -> None:
        """Una plantilla por zona y tabla (un día antes del periodo), copiada a una tabla temporal
        con su número de plantilla (``tmpl_slot``)."""
        connection = self._connection
        at = self.template_at
        async with connection.transaction():
            await self._scope()
            for scope in scopes:
                # Las filas de las que dependen las plantillas hijas: el aviso, el lote y la acción
                # de cada ``scope``.
                for sql, args in (
                    notification_row(
                        scope.notifications, at, notification_id=scope.notifications.notification_id
                    ),
                    digest_batch_row(
                        scope.notifications, at, digest_batch_id=scope.notifications.digest_batch_id
                    ),
                    corrective_action_row(scope.loop, at, action_id=scope.loop.action_id),
                ):
                    await connection.execute(sql, *args)
                for spec in TABLES:
                    pieces = [(spec.table, spec.template)]
                    if spec.child is not None:
                        pieces.append((spec.child.table, spec.child.template))
                    for table, template in pieces:
                        if table in (
                            "notifications.notification",
                            "notifications.digest_batch",
                            "loop.corrective_action",
                        ):
                            continue  # ya insertada arriba
                        sql, args = template(scope, at)
                        await connection.execute(sql, *args)
        async with connection.transaction():
            for spec in TABLES:
                specs = [(spec.table, spec.time_column)]
                if spec.child is not None:
                    specs.append((spec.child.table, spec.child.time_column))
                for table, time_column in specs:
                    name = _template_name(table)
                    # El número de plantilla sigue a la zona: el padre y el hijo de un mismo número
                    # son de la misma zona (y planta).
                    names = {column for column, _ in await _columns(connection, table)}
                    order = "zone_id" if "zone_id" in names else time_column
                    await connection.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
                    await connection.execute(
                        f"CREATE TEMP TABLE {name} AS SELECT row_number() OVER"  # noqa: S608
                        f" (ORDER BY {order}) AS tmpl_slot, * FROM {table}"
                        f" WHERE organization_id = $1 AND {time_column} = $2",
                        self._organization,
                        at,
                    )
                    (count,) = await connection.fetchrow(
                        f"SELECT count(*) FROM pg_temp.{name}"  # noqa: S608
                    )
                    if count != len(scopes):
                        raise AssertionError(
                            f"{table}: {count} plantillas, se esperaban {len(scopes)}"
                        )
                    self.result.templates[table] = int(count)

    async def clone(self, spec: TableSpec) -> None:
        rates = self._profile.rates
        per_day = spec.per_day(rates)
        columns = await _columns(self._connection, spec.table)
        child_columns = (
            await _columns(self._connection, spec.child.table) if spec.child is not None else []
        )
        started = WALL.monotonic()
        for first in range(0, self._profile.days, CHUNK_DAYS):
            last = min(self._profile.days, first + CHUNK_DAYS)
            await self._chunk(spec, columns, child_columns, per_day, first, last)
        self.result.seconds[spec.table] = round(WALL.monotonic() - started, 1)

    async def _chunk(
        self,
        spec: TableSpec,
        columns: Sequence[tuple[str, str]],
        child_columns: Sequence[tuple[str, str]],
        per_day: int,
        first: int,
        last: int,
    ) -> None:
        connection = self._connection
        template = _template_name(spec.table)
        low, high = first * per_day, last * per_day - 1
        shift, days = _shift(per_day, "b.g")
        names = ", ".join(name for name, _ in columns)
        async with connection.transaction():
            await self._scope()
            await connection.execute(
                f"CREATE TEMP TABLE batch ON COMMIT DROP AS SELECT t.tmpl_slot, g,"  # noqa: S608
                f" gen_random_uuid() AS id FROM pg_temp.{template} AS t"
                f" CROSS JOIN generate_series({low}, {high}) AS g"
            )
            key = columns[0][0] if spec.child is not None else None
            values = ", ".join(
                "b.id"
                if name == key
                else _expression(name, udt, spec.fresh, spec.overrides, shift, days)
                for name, udt in columns
            )
            await connection.execute(
                f"INSERT INTO {spec.table} ({names}) SELECT {values}"  # noqa: S608
                f" FROM pg_temp.batch AS b JOIN pg_temp.{template} AS t USING (tmpl_slot)"
            )
            if spec.child is not None:
                child = spec.child
                child_per_day = child.per_day(self._profile.rates)
                extra = child_per_day - per_day  # copias con un segundo hijo (o menos hijos)
                child_names = ", ".join(name for name, _ in child_columns)
                child_values = ", ".join(
                    "b.id"
                    if name == child.link
                    else _expression(name, udt, child.fresh, {}, shift, days, alias="c")
                    for name, udt in child_columns
                )
                if extra >= 0:
                    # Cada padre tiene un hijo, y los ``extra`` primeros del día, dos.
                    selection = f"r = 1 OR (r = 2 AND b.g % {per_day} < {extra})"
                else:
                    # Solo los primeros ``child_per_day`` padres del día tienen hijo.
                    selection = f"r = 1 AND b.g % {per_day} < {child_per_day}"
                await connection.execute(
                    f"INSERT INTO {child.table} ({child_names}) SELECT {child_values}"  # noqa: S608
                    f" FROM pg_temp.batch AS b JOIN pg_temp.{_template_name(child.table)} AS c"
                    f" USING (tmpl_slot) CROSS JOIN generate_series(1, 2) AS r WHERE {selection}"
                )


# --- Medición -------------------------------------------------------------------------------


async def _measure(
    connection: Any, generated: _Generated, profile: VolumetryProfile
) -> dict[str, Any]:
    days = profile.days
    zones = profile.zones
    per_table: dict[str, dict[str, Any]] = {}
    rows_per_day: dict[str, float] = {}
    bytes_per_table: dict[str, int] = {}
    for table in MEASURED_TABLES:
        await connection.execute(f"ANALYZE {table}")
        (rows,) = await connection.fetchrow(f"SELECT count(*) FROM {table}")  # noqa: S608
        (size,) = await connection.fetchrow(
            # Una tabla sin particionar no tiene árbol de particiones: su propio tamaño.
            "SELECT coalesce((SELECT sum(pg_total_relation_size(relid))"
            " FROM pg_partition_tree($1::regclass)), pg_total_relation_size($1::regclass))",
            table,
        )
        templates = generated.templates.get(table, 0)
        if table == "notifications.notification_source_key":
            templates = generated.templates.get("notifications.notification", 0)
        generated_rows = int(rows) - templates
        per_day = generated_rows / days
        per_row = int(size) / int(rows) if rows else 0.0
        year_rows = per_day * 365 * TARGET_ZONES / zones
        year_bytes = year_rows * per_row
        rows_per_day[table] = round(per_day, 2)
        bytes_per_table[table] = int(size)
        per_table[table] = {
            "rows": int(rows),
            "generated_rows": generated_rows,
            "rows_per_day": round(per_day, 2),
            "rows_per_zone_day": round(per_day / zones, 2),
            "bytes": int(size),
            "bytes_per_row": round(per_row, 1),
            "rows_per_day_300_zones": round(per_day * TARGET_ZONES / zones, 1),
            "bytes_per_day_300_zones": round(per_day * TARGET_ZONES / zones * per_row),
            "rows_per_year_300_zones": round(year_rows),
            "bytes_per_year_300_zones": round(year_bytes),
            "generation_seconds": generated.seconds.get(table),
        }
    groups = []
    for group in NFR_LAZ_06:
        year_rows = sum(per_table[table]["rows_per_year_300_zones"] for table in group.tables)
        year_bytes = sum(per_table[table]["bytes_per_year_300_zones"] for table in group.tables)
        groups.append(
            {
                "group": group.label,
                "tables": list(group.tables),
                "estimated_rows_per_year": group.rows_per_year,
                "measured_rows_per_year": year_rows,
                "estimated_bytes_per_year": group.bytes_per_year,
                "measured_bytes_per_year": year_bytes,
                "bytes_ratio": round(year_bytes / group.bytes_per_year, 3)
                if group.bytes_per_year
                else None,
                "trend": (
                    "sin cifra en el diseño (despreciable)"
                    if not group.bytes_per_year
                    else "dentro de la estimación"
                    if year_bytes <= group.bytes_per_year
                    else "por encima de la estimación"
                ),
            }
        )
    total = sum(item["bytes_per_year_300_zones"] for item in per_table.values())
    return {
        "loop_rows_per_day": rows_per_day,
        "loop_bytes_per_table": bytes_per_table,
        "tables": per_table,
        "nfr_laz_06": {
            "groups": groups,
            "estimated_total_bytes_per_year": NFR_LAZ_06_TOTAL_BYTES,
            "measured_total_bytes_per_year": total,
            "trend": "dentro de la estimación"
            if total <= NFR_LAZ_06_TOTAL_BYTES
            else "por encima de la estimación",
            "note": "tendencia (A-65): la comparación no decide el resultado",
        },
    }


# --- Paginación y archivado ----------------------------------------------------------------------


@dataclass
class _Traversal:
    ids: list[uuid.UUID]
    pages: int
    latencies_ms: list[float]
    rate_limited_waits: int = 0


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


class _Pagination:
    def __init__(
        self, env: AuthzEnvironment, organization_id: uuid.UUID, member: uuid.UUID, page: int
    ) -> None:
        self._env = env
        self._organization = organization_id
        self._member = member
        self._page = page
        clock = env.sessions.clock
        self.database = app_database(
            env.sessions.migrated,
            worker_pool_size=2,
            lock_timeout_ms=60_000,
            statement_timeout_ms=1_800_000,
            pool_timeout_seconds=60.0,
        )
        reader = NotificationReader(
            database=self.database,
            authorizer=env.authorizer,
            clock=clock,
            provider_organization_id=env.provider_organization_id,
        )
        self.app = World(clock=clock).app(
            runtime={
                "sessions": env.contexts,
                "authorizer": ContextAuthorizer(
                    audit=env.audit,
                    provider_organization_id=env.provider_organization_id,
                    # Solo lo consulta un contexto bajo concesión de proveedor: aquí no hay ninguno.
                    provider_queries=LedgerProviderQueryLedger(Mock()),
                    clock=clock,
                ),
                "state": {NOTIFICATIONS_STATE_KEY: NotificationsHttp(reader=reader)},
            },
        )

    def traverse(self, *, include_archived: bool, max_pages: int | None = None) -> _Traversal:
        """Página a página hasta el final (o hasta ``max_pages``), comprobando el orden."""
        cookie = self._env.open_session(self._organization, self._member)
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}

        clock = self._env.sessions.clock

        async def walk() -> _Traversal:
            ids: list[uuid.UUID] = []
            latencies: list[float] = []
            previous: tuple[str, str] | None = None
            after: str | None = None
            pages = 0
            waits = 0
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
                timeout=120.0,
            ) as client:
                while True:
                    params: dict[str, Any] = {"limit": self._page}
                    if include_archived:
                        params["include_archived"] = "true"
                    if after is not None:
                        params["after"] = after
                    # Como un cliente que respeta el límite de la sesión (600 por minuto,
                    # NFR-NUC-25): el límite mide con el reloj simulado de la aplicación.
                    clock.advance(PAGE_PACING_SECONDS)
                    began = WALL.monotonic()
                    response = await client.get("/notifications", params=params, headers=headers)
                    latencies.append((WALL.monotonic() - began) * 1000)
                    if response.status_code == 429 and waits < MAX_RATE_LIMIT_WAITS:
                        # Espera lo que pide ``retry_after_seconds`` y repite la misma página.
                        waits += 1
                        clock.advance(float(response.json()["retry_after_seconds"]))
                        continue
                    if response.status_code != 200:
                        raise AssertionError(f"GET /notifications: {response.text}")
                    body = response.json()
                    pages += 1
                    for item in body["items"]:
                        key = (item["created_at"], item["notification_id"])
                        if previous is not None and not key < previous:
                            raise AssertionError(f"orden no decreciente: {previous} → {key}")
                        previous = key
                        ids.append(uuid.UUID(item["notification_id"]))
                    after = body["next_after"]
                    if after is None or (max_pages is not None and pages >= max_pages):
                        break
            return _Traversal(ids, pages, latencies, waits)

        result: _Traversal = self._env.run(walk())
        return result

    def archive(self, now: dt.datetime) -> float:
        """``archive_read_notifications`` en una transacción de la organización (U-04)."""
        context = self._env.contexts.context_from_event(
            _EventRef(self._organization, uuid7()), unit=ActorUnit.U04
        )
        clock = SimulatedClock(now)

        async def run() -> float:
            began = WALL.monotonic()
            async with self.database.transaction(context) as transaction:
                await archive_read_notifications(transaction, clock)
            return WALL.monotonic() - began

        seconds: float = self._env.run(run())
        return seconds

    def close(self) -> None:
        self._env.run(self.database.dispose())


@dataclass(frozen=True)
class _EventRef:
    organization_id: uuid.UUID
    correlation_id: uuid.UUID


async def _seed_pagination(
    connection: Any,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    member: uuid.UUID,
    total: int,
    end: dt.datetime,
) -> None:
    """``total`` avisos de ``member`` repartidos en el año que acaba en ``end`` (de tres en tres
    con la misma marca); los de índice par, leídos una hora después de crearse."""
    span_ms = int((365 * DAY).total_seconds() * 1000)
    groups = (total + 2) // 3
    for low in range(0, total, PAGINATION_CHUNK):
        high = min(total, low + PAGINATION_CHUNK) - 1
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true)", str(organization_id)
            )
            await connection.execute("SELECT set_config('vigia.actor_kind', 'system', true)")
            await connection.execute(
                "INSERT INTO notifications.notification (notification_id, organization_id,"
                " plant_id, zone_id, event_name, source_event_id, urgency, title_es, body_es,"
                " deep_link_ref, recipient_user_id, created_at, read_at)"
                " SELECT gen_random_uuid(), $1, $2, $3, 'finding_received', gen_random_uuid(),"
                " 'normal', 'Hallazgo nuevo en la zona sintética',"
                " 'Hay un hallazgo nuevo por clasificar en la zona sintética.',"
                " jsonb_build_object('kind', 'finding', 'id', gen_random_uuid()::text), $4,"
                " created, CASE WHEN i % 2 = 0 THEN created + interval '1 hour' END"
                " FROM generate_series($5::bigint, $6::bigint) AS i,"
                " LATERAL (SELECT $7::timestamptz - make_interval(secs => ((i / 3) * $8::bigint"
                " / $9::bigint + 1) / 1000.0) AS created) AS stamp",
                organization_id,
                plant_id,
                zone_id,
                member,
                low,
                high,
                end,
                span_ms,
                groups,
            )


# --- Orquestación ---------------------------------------------------------------------------


@contextlib.contextmanager
def _superuser(env: AuthzEnvironment) -> Iterator[Any]:
    connection = env.run(env.sessions.migrated.connect())
    try:
        yield connection
    finally:
        env.run(connection.close())


def _call[T](env: AuthzEnvironment, awaitable: Awaitable[T]) -> T:
    result: T = env.run(awaitable)
    return result


def run_laz(
    endpoint: PostgresEndpoint,
    profile: VolumetryProfile,
    *,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """La volumetría de U-04 de ``profile``; devuelve el informe (sección ``u04``).

    Falla (``AssertionError``) si la paginación pierde o repite un aviso o si el archivado borra
    alguna fila; las cifras de NFR-LAZ-06 no deciden nada."""
    began = WALL.monotonic()
    with authz_environment(endpoint, "volumetria_laz") as env:
        site = env.add_site(plants=profile.zones, zones_per_plant=1)
        organization = site.organization_id
        owner = env.add_user(organization)
        member = env.add_user(organization)
        env.assign(organization, member, Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION)
        (row,) = env.fetch("SELECT now() AS now, date_trunc('day', now()) AS today")
        # Un solo reloj (retro 14 y 15): el simulado de las sesiones, desde la hora de la base; el
        # archivado y su umbral salen de él.
        env.sessions.clock.set(to_millisecond(row["now"]))
        end = row["today"]
        first_day = end - profile.days * DAY
        scopes: list[_Scopes] = []
        for plant, zones in site.plants.items():
            node = uuid.uuid4()
            env.execute(
                "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
                " status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                node,
                organization,
                plant,
                f"ND-{node.hex[:10].upper()}",
                first_day - 2 * DAY,
            )
            (zone,) = zones
            scopes.append(
                _Scopes(
                    LoopScope(organization, plant, zone, node, owner, uuid.uuid4()),
                    NotificationScope(
                        organization,
                        plant,
                        zone,
                        owner,
                        notification_created_at=first_day - DAY,
                    ),
                    AccreditationScope(organization, plant, zone, owner),
                )
            )
        with _superuser(env) as connection:

            async def partitions() -> None:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('vigia.actor_kind', 'system', true)"
                    )
                    for function in (
                        "vigia_create_loop_month_partitions",
                        "vigia_create_notifications_month_partitions",
                    ):
                        rows = await connection.fetch(
                            f"SELECT * FROM shared.{function}($1, $2)",  # noqa: S608
                            (first_day - 2 * DAY).date().replace(day=1),
                            end.date().replace(day=1),
                        )
                        blocked = [row["partition"] for row in rows if row["blocked"]]
                        if blocked:
                            raise AssertionError(f"particiones bloqueadas: {blocked}")

            _call(env, partitions())
            generator = _Generator(connection, organization, profile, first_day)
            _call(env, generator.templates(scopes))
            for spec in TABLES:
                _call(env, generator.clone(spec))
                log(f"[laz] {spec.table}: {generator.result.seconds[spec.table]} s")
            measured = _call(env, _measure(connection, generator.result, profile))
            log("[laz] métricas de NFR-LAZ-06 medidas")
            plant, zone = scopes[0].loop.plant_id, scopes[0].loop.zone_id
            seeded = WALL.monotonic()
            _call(
                env,
                _seed_pagination(
                    connection, organization, plant, zone, member, profile.notifications, end
                ),
            )
            seed_seconds = WALL.monotonic() - seeded

            def count(condition: str = "TRUE", *args: Any) -> int:
                rows = env.fetch(
                    "SELECT count(*) AS n FROM notifications.notification"  # noqa: S608
                    f" WHERE recipient_user_id = $1 AND {condition}",
                    member,
                    *args,
                )
                return int(rows[0]["n"])

            now = env.now()
            total_before = count()
            expected_archived = count("read_at <= $2", now - ARCHIVE_AFTER_READ)
            all_ids = {
                uuid.UUID(str(item["notification_id"]))
                for item in env.fetch(
                    "SELECT notification_id FROM notifications.notification"
                    " WHERE recipient_user_id = $1",
                    member,
                )
            }
        pagination = _Pagination(env, organization, member, profile.page_size)
        try:
            # Antes del archivado ningún aviso está archivado: el listado por defecto los recorre
            # todos (y usa el índice parcial ``notification_recipient_list``).
            before = pagination.traverse(include_archived=False)
            log(f"[laz] recorrido 1: {before.pages} páginas")
            archive_seconds = pagination.archive(now)
            total_after = count()
            archived = count("archived_at IS NOT NULL")
            after_default = pagination.traverse(include_archived=False)
            log(f"[laz] recorrido 2 (sin lo archivado): {after_default.pages} páginas")
            # Con lo archivado, solo las primeras páginas como medición: el índice de la lista es
            # parcial (``archived_at IS NULL``) y cada página ordena todos los avisos del usuario.
            with_archived = pagination.traverse(
                include_archived=True, max_pages=INCLUDE_ARCHIVED_PAGES
            )
        finally:
            pagination.close()
        visible = {
            uuid.UUID(str(item["notification_id"]))
            for item in env.fetch(
                "SELECT notification_id FROM notifications.notification"
                " WHERE recipient_user_id = $1 AND archived_at IS NULL",
                member,
            )
        }
    checks = {
        "seeded": total_before == profile.notifications,
        "first_walk_exact": _exact(before.ids, all_ids),
        "archive_deletes_nothing": total_after == total_before,
        "archived_are_the_old_read": archived == expected_archived > 0,
        "default_walk_exact": _exact(after_default.ids, visible),
        "visible_are_the_rest": len(visible) == total_before - expected_archived,
    }
    latencies = before.latencies_ms + after_default.latencies_ms
    report = {
        "nfr": ["NFR-LAZ-06", "NFR-LAZ-09"],
        "profile": dataclasses.asdict(profile),
        "period": {"from": first_day.isoformat(), "to": end.isoformat()},
        **measured,
        "pagination": {
            "rows": total_before,
            "page_size": profile.page_size,
            "pages_before_archive": before.pages,
            "pages_after_archive_default": after_default.pages,
            "seen_before_archive": len(before.ids),
            "seen_after_archive_default": len(after_default.ids),
            "page_ms_p50": _percentile(latencies, 0.5),
            "page_ms_p95": _percentile(latencies, 0.95),
            "rate_limited_waits": before.rate_limited_waits
            + after_default.rate_limited_waits
            + with_archived.rate_limited_waits,
            "include_archived_pages": with_archived.pages,
            "include_archived_page_ms_p50": _percentile(with_archived.latencies_ms, 0.5),
            "include_archived_page_ms_max": _percentile(with_archived.latencies_ms, 1.0),
            "seed_seconds": round(seed_seconds, 1),
        },
        "archive": {
            "rows_before": total_before,
            "rows_after": total_after,
            "archived": archived,
            "expected_archived": expected_archived,
            "seconds": round(archive_seconds, 1),
        },
        "checks": checks,
        "seconds": round(WALL.monotonic() - began, 1),
    }
    return report


def _exact(seen: Sequence[uuid.UUID], expected: set[uuid.UUID]) -> bool:
    """Sin pérdidas ni repeticiones: tantos como se esperan, todos distintos y los mismos."""
    return len(seen) == len(expected) and len(set(seen)) == len(seen) and set(seen) == expected


def report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
