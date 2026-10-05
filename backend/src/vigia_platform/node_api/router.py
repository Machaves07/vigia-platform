"""Enrutador del contrato sobre el esqueleto de U-01 y verificación previa común (TASK-206).

**Mecanismo de declaración** (A-51): el esqueleto (``create_router``) no se edita. Se construye
con ``NodeOperations``, la implementación de ``IngestOperations`` que delega en el manejador de
cada ruta, y cada una de sus rutas se incluye **por separado** bajo ``/api/nodes`` con la
dependencia ``node_route(NodeRoute.X)`` resuelta por su ``operation_id``. Solo se montan las
rutas que una tarea publica (``node_router(routes)``; en producción ``PUBLISHED_NODE_ROUTES`` de
``shared.runtime.units``): ``GET conformance-profile`` nunca (no se implementa en v1) y una
operación de negocio aún sin tarea, tampoco, así que la especificación ``app.yaml`` solo cambia
cuando una tarea publica su ruta. El manejador de cada ruta (``NodeOperation``) lo construye la
raíz con sus servicios y vive en la ``NodeApiGate`` de ``app.state``.

**Verificación previa** (``NodeApiGate.admit``, la ``NodeGate`` que la declaración llama), en un
orden fijo (BR-GOB-84, parte común de PR-GOB-02):

0. admisión: freno global y límite de tasa por nodo (el ``node_id`` del certificado, sin tocar la
   base) o, en el alta, por origen (``limits``);
1. **versión**: ``X-Vigia-Contract-Version`` con la función de compatibilidad de U-01
   (``versioning``);
2. **certificado y alcance**: identidad del nodo en una consulta sin caché (``identity``) y, si
   la ruta nombra una zona (``zone_id``), que esté entre las asignadas al nodo
   (``node_zone_mismatch``); el alta no lleva certificado;
3. **tamaño**: el exceso que marcó el límite de cuerpo de la cadena (``payload_too_large``), la
   codificación admitida por la operación y el ``gzip`` descomprimido con tope (nunca se infla más
   allá del límite más un byte);
4. **esquema**: el lector estricto de U-01 (``schema_invalid`` con ``field`` = ruta JSON) y las
   cabeceras y parámetros de la operación (``Idempotency-Key`` única, ``clip_id`` UUID v7).

Entre (3) y (4), una operación puede declarar ``before_schema`` (sin consulta) y ``check_body``
(con consulta): la parte del alcance que depende del cuerpo (TASK-221: organización, planta, nodo y
zona del contenido asignada en el instante del hecho), para que el paso 2 gane al 4. El resultado
queda en ``RequestState.node`` (``NodeRequest``) y la operación lo recibe. ``on_rejection`` recibe
todo fallo posterior a resolver la identidad del nodo (y el de la versión, resolviéndola solo para
eso) antes de responderlo: la ingesta deja así el rastro de cada rechazo permanente (BR-GOB-96).
"""

from __future__ import annotations

import json
import re
import uuid
import zlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from opentelemetry import trace as otel_trace
from starlette.responses import JSONResponse
from vigia_contracts.models._base import ContractModel
from vigia_contracts.models.enumerations import CompatibilityResult, RejectionCode
from vigia_contracts.server_skeleton import create_router

from vigia_platform.identity.authz.context import NodeContextRejected, NodeScope, PresentedNode
from vigia_platform.node_api.declarations import NodeOperationSpec, PathParameter, spec_of
from vigia_platform.node_api.identity import NodeIdentity
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import ROUTE_SPAN, NodeResponses, NodeResult
from vigia_platform.node_api.rejections import NodeRejection, NotFound
from vigia_platform.node_api.versioning import (
    CONTRACT_VERSION_HEADER,
    VersionPolicy,
    check_version,
)
from vigia_platform.shared.api.declarations import (
    NODE_GATE_STATE_KEY,
    NODE_PREFIX,
    NodeRoute,
    node_route,
)
from vigia_platform.shared.api.request_state import request_state
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.tracing import TRACER_NAME

__all__ = [
    "NodeApiGate",
    "NodeAttempt",
    "NodeOperation",
    "NodeOperations",
    "NodeReply",
    "NodeRequest",
    "node_router",
]

IDEMPOTENCY_HEADER: Final = "idempotency-key"
_UUID7: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class NodeAttempt:
    """Lo que se sabe de la petición antes de terminar la verificación (para el registro)."""

    route: NodeRoute
    presented: PresentedNode | None


@dataclass(frozen=True, slots=True)
class NodeRequest:
    """Una petición de nodo que pasó la verificación previa común: lo que recibe la operación."""

    route: NodeRoute
    correlation_id: uuid.UUID
    received_at: datetime
    compatibility_result: CompatibilityResult
    """``accepted`` o ``accepted_with_notice`` (el latido lo informa en ``contract_notice``)."""
    node: NodeScope | None
    """El contexto y el alcance del nodo; ``None`` solo en el alta (sin certificado)."""
    presented: PresentedNode | None
    body: bytes
    """El cuerpo descomprimido (``b""`` en las operaciones sin cuerpo)."""
    document: ContractModel | None
    """El cuerpo validado con el modelo estricto de U-01."""
    idempotency_key: str | None = None
    path: Mapping[str, str] = field(default_factory=dict)
    contract_version: str | None = None
    """El valor de ``X-Vigia-Contract-Version`` que pasó el paso 1 (TASK-221 lo compara con
    ``contract_version`` del cuerpo)."""


@dataclass(frozen=True, slots=True)
class NodeReply:
    """La respuesta correcta de una operación (``200``)."""

    model: ContractModel | None = None
    content: bytes | None = None
    """JSON ya serializado (el sobre del catálogo, byte a byte, TASK-223)."""
    duplicate: bool = False
    """``accepted_duplicate`` (BR-CTR-27): misma respuesta, otra métrica."""


type BeforeSchema = Callable[[NodeScope | None, object], None]
type CheckBody = Callable[[NodeScope, object, datetime], Awaitable[None]]
type OnRejection = Callable[[NodeScope, BaseException, datetime], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class NodeOperation:
    """El manejador de negocio de una ruta del contrato (TASK-219, 221, 222, 223, 226)."""

    handle: Callable[[NodeRequest], Awaitable[NodeReply]]
    before_schema: BeforeSchema | None = None
    """La parte del alcance que depende del cuerpo (JSON sin validar o ``None``)."""
    check_body: CheckBody | None = None
    """Como ``before_schema``, pero con consulta (TASK-221: la zona asignada en el instante del
    hecho): recibe el alcance del nodo, el JSON sin validar y el instante de recepción."""
    on_rejection: OnRejection | None = None
    """Se llama con cualquier fallo de la petición una vez resuelta la identidad del nodo (también
    el de la versión: la identidad se resuelve entonces solo para esto), antes de responderlo
    (TASK-221: la auditoría de todo rechazo permanente, BR-GOB-96). Lo que lance sustituye al
    fallo (fallo cerrado: sin rastro, transitorio)."""


# --- Verificación previa -------------------------------------------------------------------------


def _client_address(scope: MutableMapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, tuple | list) and client and isinstance(client[0], str):
        return client[0]
    return "unknown"


def _decompress(raw: bytes, encoding: str, limit: int) -> bytes:
    """El cuerpo descomprimido con tope: nunca produce más de ``limit + 1`` bytes."""
    if encoding == "identity":
        if len(raw) > limit:
            raise NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)
        return raw
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = decoder.decompress(raw, limit + 1)
    except zlib.error:
        raise NodeRejection(
            RejectionCode.SCHEMA_INVALID, field="Content-Encoding", body_level=True
        ) from None
    if len(data) > limit:
        raise NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)
    if not decoder.eof or decoder.unused_data:
        raise NodeRejection(RejectionCode.SCHEMA_INVALID, field="Content-Encoding", body_level=True)
    return data


def _loose_json(body: bytes) -> object:
    """El JSON del cuerpo sin validar (para ``before_schema``), o ``None``."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


class NodeApiGate:
    """``NodeGate`` de la aplicación: la verificación previa común de BR-GOB-84.

    Guarda también los manejadores de negocio de cada ruta (``operations``, construidos con los
    servicios de la raíz) y las respuestas de la clase ``node``: ``NodeOperations`` los busca en
    ``app.state`` en cada petición, así que el enrutador no recibe dependencias.
    """

    def __init__(
        self,
        *,
        identity: NodeIdentity,
        limits: NodeRateLimits,
        clock: Clock,
        responses: NodeResponses,
        policy: VersionPolicy | None = None,
        operations: Mapping[NodeRoute, NodeOperation] | None = None,
    ) -> None:
        self._identity = identity
        self._limits = limits
        self._clock = clock
        self._responses = responses
        self._policy = policy if policy is not None else VersionPolicy()
        self._operations = dict(operations or {})

    @property
    def identity(self) -> NodeIdentity:
        return self._identity

    @property
    def limits(self) -> NodeRateLimits:
        return self._limits

    @property
    def responses(self) -> NodeResponses:
        return self._responses

    def operation(self, route: NodeRoute) -> NodeOperation | None:
        """El manejador de negocio de ``route``, si alguna tarea lo registró."""
        return self._operations.get(route)

    async def admit(self, request: Request, route: NodeRoute) -> None:
        """Pasos 0 a 4; deja ``NodeRequest`` en el estado o lanza el primer rechazo."""
        route = NodeRoute(route)
        spec = spec_of(route)
        scope = request.scope
        state = request_state(scope)
        headers = request.headers
        now = self._clock.now()
        correlation_id = state.correlation_id or uuid7(self._clock)
        presented: PresentedNode | None = None
        certificate_rejection: NodeRejection | None = None
        if route.mutual_tls:
            try:
                presented = self._identity.presented(headers, now)
            except NodeRejection as rejection:
                certificate_rejection = rejection
        state.node = NodeAttempt(route, presented)
        # (0) admisión: freno y tasa (la ficha se consume aunque después se rechace).
        await self._limits.admit(
            route,
            node_id=presented.node_id if presented is not None else None,
            address=_client_address(scope),
        )
        operation = self._operations.get(route)
        versions = headers.getlist(CONTRACT_VERSION_HEADER)
        # (1) versión.
        try:
            result = check_version(versions, self._policy, now)
        except NodeRejection as rejection:
            if certificate_rejection is None and presented is not None:
                await self._version_rejected(operation, presented, correlation_id, rejection, now)
            raise
        # (2) certificado y alcance.
        node: NodeScope | None = None
        if route.mutual_tls:
            if certificate_rejection is not None or presented is None:
                raise certificate_rejection or NodeRejection(RejectionCode.NODE_NOT_ENROLLED)
            node = await self._identity.resolve(presented, correlation_id)
        try:
            if node is not None and spec.path_parameter is PathParameter.ZONE_ID:
                self._require_zone(node, request.path_params.get("zone_id"))
            # (3) tamaño.
            body = await self._body(request, spec)
            loose = _loose_json(body)
            if operation is not None and operation.before_schema is not None:
                operation.before_schema(node, loose)
            if operation is not None and operation.check_body is not None and node is not None:
                await operation.check_body(node, loose, now)
            # (4) esquema.
            document = spec.parser(body) if spec.parser is not None else None
            idempotency_key = self._idempotency_key(spec, headers.getlist(IDEMPOTENCY_HEADER))
            if spec.path_parameter is PathParameter.CLIP_ID:
                clip_id = request.path_params.get("clip_id")
                if not isinstance(clip_id, str) or _UUID7.fullmatch(clip_id) is None:
                    raise NodeRejection(RejectionCode.SCHEMA_INVALID, field="clip_id")
        except Exception as error:
            if node is not None and operation is not None and operation.on_rejection is not None:
                await operation.on_rejection(node, error, now)
            raise
        state.node = NodeRequest(
            route=route,
            correlation_id=correlation_id,
            received_at=now,
            compatibility_result=result,
            node=node,
            presented=presented,
            body=body,
            document=document,
            idempotency_key=idempotency_key,
            path={key: str(value) for key, value in request.path_params.items()},
            contract_version=versions[0],
        )

    async def _version_rejected(
        self,
        operation: NodeOperation | None,
        presented: PresentedNode,
        correlation_id: uuid.UUID,
        rejection: NodeRejection,
        now: datetime,
    ) -> None:
        """El paso 1 rechazó: la operación que lo pide recibe el rechazo con la identidad del nodo.

        Sin identidad (nodo no dado de alta o revocado) no hay organización donde registrarlo.
        """
        if operation is None or operation.on_rejection is None:
            return
        try:
            node = await self._identity.resolve(presented, correlation_id)
        except NodeContextRejected:
            return
        await operation.on_rejection(node, rejection, now)

    @staticmethod
    def _require_zone(node: NodeScope, value: object) -> None:
        """La zona de la ruta tiene que ser una de las asignadas al nodo ahora (BR-GOB-88)."""
        if not isinstance(value, str) or _UUID.fullmatch(value) is None:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id")
        if not node.covers_zone(uuid.UUID(value)):
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id")

    async def _body(self, request: Request, spec: NodeOperationSpec) -> bytes:
        state = request_state(request.scope)
        if state.body_length_invalid:
            raise NodeRejection(
                RejectionCode.SCHEMA_INVALID, field="Content-Length", body_level=True
            )
        if state.body_exceeded:
            raise NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)
        raw = await request.body()
        encodings = request.headers.getlist("content-encoding")
        if len(encodings) > 1:
            raise NodeRejection(
                RejectionCode.SCHEMA_INVALID, field="Content-Encoding", body_level=True
            )
        encoding = encodings[0].strip().lower() if encodings else "identity"
        if encoding != "identity" and encoding not in spec.content_encodings:
            raise NodeRejection(
                RejectionCode.SCHEMA_INVALID, field="Content-Encoding", body_level=True
            )
        return _decompress(raw, encoding, spec.max_body_bytes)

    @staticmethod
    def _idempotency_key(spec: NodeOperationSpec, values: list[str]) -> str | None:
        if "Idempotency-Key" not in spec.operation.header_parameters:
            return None
        if len(values) > 1:
            raise NodeRejection(
                RejectionCode.SCHEMA_INVALID, field="Idempotency-Key", body_level=True
            )
        return values[0] if values else None


# --- Operaciones del esqueleto -------------------------------------------------------------------


class NodeOperations:
    """``IngestOperations`` de U-01: cada operación delega en el manejador que registró su tarea.

    El manejador y las respuestas salen de la ``NodeApiGate`` de ``app.state`` en cada petición
    (la misma que la declaración ``node_route`` acaba de llamar).
    """

    def __init__(self, *, tracer: otel_trace.Tracer | None = None) -> None:
        self._tracer = tracer if tracer is not None else otel_trace.get_tracer(TRACER_NAME)

    async def _dispatch(self, route: NodeRoute, request: Request) -> Response:
        scope = request.scope
        state = request_state(scope)
        gate = getattr(request.app.state, NODE_GATE_STATE_KEY, None)
        if not isinstance(gate, NodeApiGate):
            # Sin verificación instalada la declaración ya denegó; nunca se llega aquí.
            raise RuntimeError("ruta del contrato sin NodeApiGate")
        responses = gate.responses
        with self._tracer.start_as_current_span(
            ROUTE_SPAN,
            attributes={
                "route": route.path,
                "correlation_id": str(state.correlation_id or ""),
            },
        ):
            node_request = state.node
            operation = gate.operation(route)
            try:
                if operation is None:
                    raise NotFound()
                if not isinstance(node_request, NodeRequest) or node_request.route is not route:
                    # La declaración no dejó pasar la verificación previa: fallo cerrado.
                    raise RuntimeError("operación de nodo sin verificación previa")
                reply = await operation.handle(node_request)
            except Exception as error:
                return responses.render(error, scope)
            if reply.content is not None:
                response: Response = Response(
                    content=reply.content, status_code=200, media_type="application/json"
                )
            elif reply.model is not None:
                response = JSONResponse(reply.model.to_json_value(), status_code=200)
            else:
                return responses.render(RuntimeError("respuesta vacía"), scope)
            response.headers["Cache-Control"] = "no-store"
            result = NodeResult.ACCEPTED_DUPLICATE if reply.duplicate else NodeResult.ACCEPTED
            responses.record(scope, route, 200, result, None)
            return response

    async def post_clip_upload(
        self, request: Request, *, x_vigia_contract_version: str | None
    ) -> Response:
        return await self._dispatch(NodeRoute.CLIP_UPLOAD, request)

    async def post_clip_upload_confirmation(
        self, request: Request, clip_id: str, *, x_vigia_contract_version: str | None
    ) -> Response:
        return await self._dispatch(NodeRoute.CLIP_CONFIRMATION, request)

    async def get_conformance_profile(
        self, request: Request, *, x_vigia_contract_version: str | None
    ) -> Response:  # pragma: no cover - no se monta (A-51)
        raise NotFound()

    async def post_credential_rotation(
        self, request: Request, *, x_vigia_contract_version: str | None
    ) -> Response:
        return await self._dispatch(NodeRoute.CREDENTIAL_ROTATION, request)

    async def post_detection_review(
        self,
        request: Request,
        *,
        x_vigia_contract_version: str | None,
        idempotency_key: str | None,
    ) -> Response:
        return await self._dispatch(NodeRoute.DETECTION_REVIEW, request)

    async def post_enrollment(
        self, request: Request, *, x_vigia_contract_version: str | None
    ) -> Response:
        return await self._dispatch(NodeRoute.ENROLLMENT, request)

    async def post_finding(
        self,
        request: Request,
        *,
        x_vigia_contract_version: str | None,
        idempotency_key: str | None,
    ) -> Response:
        return await self._dispatch(NodeRoute.FINDING, request)

    async def post_heartbeat(
        self,
        request: Request,
        *,
        x_vigia_contract_version: str | None,
        idempotency_key: str | None,
    ) -> Response:
        return await self._dispatch(NodeRoute.HEARTBEAT, request)

    async def post_observability_event(
        self,
        request: Request,
        *,
        x_vigia_contract_version: str | None,
        idempotency_key: str | None,
    ) -> Response:
        return await self._dispatch(NodeRoute.OBSERVABILITY_EVENT, request)

    async def post_update_result(
        self,
        request: Request,
        *,
        x_vigia_contract_version: str | None,
        idempotency_key: str | None,
    ) -> Response:
        return await self._dispatch(NodeRoute.UPDATE_RESULT, request)

    async def get_zone_catalog(
        self, request: Request, zone_id: str, *, x_vigia_contract_version: str | None
    ) -> Response:
        return await self._dispatch(NodeRoute.ZONE_CATALOG, request)


_BY_OPERATION_ID: Final[Mapping[str, NodeRoute]] = {
    route.operation_id: route for route in NodeRoute
}


def node_router(routes: Iterable[NodeRoute]) -> APIRouter:
    """Las rutas ``routes`` del esqueleto de U-01, bajo ``/api/nodes`` (sin dependencias).

    Cada ruta se incluye por separado con su ``node_route`` (resuelta por ``operation_id``): la
    declaración llama a la ``NodeGate`` de la aplicación antes de la operación. Una ruta que no
    está en ``routes`` no se monta; ``conformance-profile`` no tiene ``NodeRoute`` y nunca se
    monta (A-51).
    """
    selected = {NodeRoute(route) for route in routes}
    skeleton = create_router(NodeOperations())
    router = APIRouter()
    for route in skeleton.routes:
        if not isinstance(route, APIRoute):
            continue
        node = _BY_OPERATION_ID.get(route.operation_id or "")
        if node is None or node not in selected:
            continue
        single = APIRouter()
        single.routes.append(route)
        router.include_router(single, prefix=NODE_PREFIX, dependencies=[node_route(node)])
    return router
