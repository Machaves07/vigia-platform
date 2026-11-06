"""``vigia-admin node-configuration show|set`` (TASK-544; D-11; Q7).

Las subórdenes viven aquí para no engordar ``admin_cli``, que solo registra este módulo: la lógica
y sus garantías son de ``node_configuration_write``. La salida es siempre una línea JSON (``--json``
se admite para decirlo en la orden), con el ``mute_after_seconds`` resultante.

``--operator`` es obligatorio también en ``show``: la orden lee datos de una organización cliente y
el actor debe ser un ``platform_operator`` activo.
"""

from __future__ import annotations

import argparse
from typing import Any

from vigia_platform.fleet.application.node_configuration_write import (
    NodeConfigurationChanges,
    NodeConfigurationResult,
    NodeConfigurationWriter,
)
from vigia_platform.fleet.domain.node_configuration import NodeConfigurationInvalid
from vigia_platform.identity.application.admin_cli import (
    AdminConfig,
    AdminError,
    AdminRuntime,
    Streams,
    _add_common,
    _add_operator,
    _emit,
    _operator_context,
    _Parser,
    _require_provider,
    _uuid_argument,
)

__all__ = ["add_parser", "run"]


def _add_node(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--node", required=True, type=_uuid_argument, metavar="UUID", help="identificador del nodo"
    )
    parser.add_argument("--json", action="store_true", help="la salida es siempre una línea JSON")
    _add_operator(parser)


def add_parser(commands: Any) -> None:
    """Registra ``node-configuration`` y sus dos acciones en el analizador de ``vigia-admin``."""
    parser = commands.add_parser(
        "node-configuration",
        help="configuración de un nodo: fuentes de tiempo, latido, retención y token",
        description=(
            "Lee o cambia fleet.node_configuration de un nodo (D-11). El nodo la recibe en el\n"
            "alta y, el intervalo del latido, en la respuesta del latido siguiente."
        ),
    )
    actions = parser.add_subparsers(
        dest="node_configuration_action",
        required=True,
        metavar="ACCIÓN",
        title="acciones",
        parser_class=_Parser,
    )
    show = actions.add_parser("show", help="muestra la configuración que recibe el nodo")
    _add_node(show)
    change = actions.add_parser(
        "set",
        help="cambia los campos nombrados; los demás conservan su valor",
        description=(
            "Valida con el dominio: de 1 a 8 fuentes, intervalo de 15 a 600 s, retención de 1 a\n"
            "90 días y token de 600 a 3 600 s. Escribe una entrada node_configuration_changed en\n"
            "la auditoría de la proveedora. mute_after_seconds se deriva: 5 veces el intervalo."
        ),
    )
    _add_node(change)
    change.add_argument(
        "--time-source",
        action="append",
        dest="time_sources",
        metavar="ANFITRIÓN",
        help="fuente de tiempo; repite la opción para varias (sustituye la lista entera)",
    )
    change.add_argument("--heartbeat-interval", type=int, metavar="SEGUNDOS")
    change.add_argument("--retention-days", type=int, metavar="DÍAS")
    change.add_argument("--token-max-age", type=int, metavar="SEGUNDOS")
    _add_common(change)


def _changes(args: argparse.Namespace) -> NodeConfigurationChanges:
    return NodeConfigurationChanges(
        time_sources=None if args.time_sources is None else tuple(args.time_sources),
        sent_records_retention_days=args.retention_days,
        token_max_age_seconds=args.token_max_age,
        heartbeat_interval_seconds=args.heartbeat_interval,
    )


def _output(action: str, result: NodeConfigurationResult) -> dict[str, object]:
    configuration = result.configuration
    return {
        "command": "node-configuration",
        "action": action,
        "node_id": str(result.node.node_id),
        "organization_id": str(result.node.organization_id),
        "plant_id": str(result.node.plant_id),
        "stored": result.stored,
        "time_sources": list(configuration.time_sources),
        "sent_records_retention_days": configuration.sent_records_retention_days,
        "token_max_age_seconds": configuration.token_max_age_seconds,
        "heartbeat_interval_seconds": configuration.heartbeat_interval_seconds,
        "mute_after_seconds": configuration.mute_after_seconds,
    }


async def run(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    writer = NodeConfigurationWriter(
        database=runtime.database,
        contexts=runtime.contexts,
        audit=runtime.audit,
        clock=runtime.clock,
        provider_organization_id=_require_provider(config),
    )
    operator = await _operator_context(args, runtime)
    try:
        if args.node_configuration_action == "show":
            _emit(streams, _output("show", await writer.show(args.node, operator)))
            return
        changes = _changes(args)
        if not changes.named():
            raise AdminError("invalid_arguments", "nombra al menos un campo que cambiar")
        result = await writer.set_node_configuration(
            args.node, changes, operator, dry_run=args.dry_run
        )
    except NodeConfigurationInvalid as error:
        raise AdminError("invalid_value", str(error), field_path=error.field) from error
    document = _output("set", result)
    document["dry_run"] = args.dry_run
    if result.receipt is not None:
        document["audit_entry_id"] = str(result.receipt.entry_id)
        document["chain_sequence"] = result.receipt.chain_sequence
    _emit(streams, document)
