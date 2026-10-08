"""FS-GOB-10 · Instancia con el cubo de fichas frío tras un reinicio (NFR-GOB-15, 33, 49; PR-GOB-29;
riesgo R10; LC-GOB-19, LC-GOB-20).

Sobre la plataforma de producción con **dos procesos ``vigia-api`` de verdad** (la orden de la
imagen, con todas las unidades) tras un balanceador que solo enruta a los que responden
``/health/ready`` y, delante, el balanceador ``nodes.`` con mTLS
(``gob_support.restartable_platform``; la autoridad de nodos efímera es la de ``vigia-admin
bootstrap``). La flota se da de alta por las rutas del contrato y la mueve el **nodo simulado del
kit de U-01** (``tests.load.driver``) a **ritmo real** (``speed_factor`` 1), en un proceso aparte.

**Inyección**: **reinicio rodante** de los dos procesos mientras 100 nodos envían (los instantes
salen de la semilla): cada uno se retira del balanceador, se para con ``SIGTERM``, arranca de
nuevo con su cubo de fichas en memoria **frío** y vuelve al balanceador. Después, con los dos
cubos recién estrenados, una ráfaga de concesiones de un mismo nodo muy por encima de su límite.

**Resultado esperado**:

- en operación normal, **ningún nodo recibe ``rate_limited``** (ni antes, ni durante, ni después
  de los reinicios) y nada se pierde ni se duplica: aceptado = emitido, sin cola muerta, sin
  ``source_key`` repetida;
- el **límite efectivo nunca queda por debajo del mínimo de NFR-CTR-02**: en la ráfaga, las
  concesiones aceptadas en su primer minuto son al menos 240;
- cuando sí se limita, ``retry_after_seconds`` está **entre 1 y 60** (y la cabecera
  ``Retry-After`` dice lo mismo).

Con ``VIGIA_LOAD_SCALE=smoke``, 20 nodos (para ensayar el escenario en un PC compartido).
Solo datos generados.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import secrets
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.clock import SystemClock as ContractClock
from vigia_contracts.credentials import FileCredentialStore, NodeCredentials, NodeIdentity
from vigia_contracts.versioning import CONTRACT_VERSION

from tests.conformance.platform_target import INGEST_PATH
from tests.factories import uuid7
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.load.conftest import (  # noqa: F401
    clip_cache,
    load_seed,
    run_profile,
    sealed_dataset,
)
from tests.load.profiles import scaled
from tests.load.provision import LIVE_VIEW_HOST, SOFTWARE_VERSION, Fleet
from tests.load.report import analyse
from tests.load.test_load_ci import DRIVER_MARGIN_SECONDS, ledger_check
from tests.resilience.gob_support import (
    API_PROCESSES,
    RestartablePlatform,
    restartable_platform,
)
from tests.resilience.harness import WALL, scenario
from vigia_platform.node_api.limits import MINIMUM_PER_MINUTE

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

BURST: Final = 400
"""Concesiones de la ráfaga final de un mismo nodo, en segundos: más que la ráfaga de los dos
procesos (2 por 120) más lo que reponen mientras dura."""
BURST_CONCURRENCY: Final = 8
"""Por debajo del pool de nodo de cada proceso (10): la ráfaga mide el límite, no satura la base."""
GRANT_MINIMUM: Final = MINIMUM_PER_MINUTE["clip_upload"]
"""El mínimo de NFR-CTR-02 para las concesiones: 240 por minuto y nodo."""


@pytest.fixture(scope="module")
def platform(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RestartablePlatform]:
    directory = tmp_path_factory.mktemp("fs-gob-10")  # fuera del árbol, nunca versionado
    with restartable_platform(postgres_endpoint, localstack_endpoint, directory) as built:
        yield built


def _camera(payload: Any) -> str:
    """El primer ``camera_id`` del catálogo publicado de la zona."""
    if isinstance(payload, dict):
        if "camera_id" in payload:
            return str(payload["camera_id"])
        values: Any = payload.values()
    elif isinstance(payload, list):
        values = payload
    else:
        values = ()
    for value in values:
        with_camera = _camera(value) if isinstance(value, dict | list) else ""
        if with_camera:
            return with_camera
    return ""


def _burst(platform: RestartablePlatform, fleet: Fleet) -> dict[str, Any]:
    """``BURST`` concesiones del primer nodo por ``nodes.`` con su certificado, a la vez."""
    node = fleet.nodes[0]
    credentials = NodeCredentials(
        NodeIdentity(str(node.node_id), str(fleet.organization_id), str(node.plant_id)),
        FileCredentialStore(node.credential_file),
        ContractClock(),
        live_view_host=LIVE_VIEW_HOST,
        software_version=SOFTWARE_VERSION,
    )
    context = credentials.client_ssl_context(cafile=platform.tls.ca_file)
    zone = str(node.zone_ids[0])
    camera = _camera(node.catalogs[0])
    url = platform.nodes.url + INGEST_PATH + "/clip-uploads"
    outcomes: list[tuple[float, int, Any, str | None]] = []

    async def one(client: httpx.AsyncClient, gate: asyncio.Semaphore) -> None:
        data = secrets.token_bytes(64)
        body = {
            "clip_id": str(uuid7()),
            "camera_id": camera,
            "zone_id": zone,
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": 10_000,
            "purpose": "evidence",
        }
        async with gate:
            response = await client.post(
                url, json=body, headers={"X-Vigia-Contract-Version": str(CONTRACT_VERSION)}
            )
        retry = response.json().get("retry_after_seconds") if response.status_code == 429 else None
        outcomes.append(
            (WALL.monotonic(), response.status_code, retry, response.headers.get("retry-after"))
        )

    async def go() -> float:
        gate = asyncio.Semaphore(BURST_CONCURRENCY)
        async with httpx.AsyncClient(verify=context, timeout=60.0) as client:
            started = WALL.monotonic()
            await asyncio.gather(*(one(client, gate) for _ in range(BURST)))
            return started

    started = asyncio.run(go())
    first_minute = [o for o in outcomes if o[0] - started <= 60.0]
    limited = [o for o in outcomes if o[1] == 429]
    return {
        "camera": camera,
        "statuses": dict(Counter(o[1] for o in outcomes)),
        "accepted_first_minute": sum(1 for o in first_minute if o[1] == 200),
        "requests_first_minute": len(first_minute),
        "limited": len(limited),
        "retry_after_seconds": sorted({o[2] for o in limited}),
        "retry_after_header_matches": all(str(o[2]) == o[3] for o in limited),
        "seconds": round(max((o[0] for o in outcomes), default=started) - started, 2),
    }


def test_fs_gob_10_rolling_restart_with_cold_token_buckets(
    platform: RestartablePlatform,
    sealed_dataset: Path,  # noqa: F811
    clip_cache: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    profile = scaled("fs-gob-10")
    with scenario(
        "FS-GOB-10",
        title="Instancia con el cubo de fichas frío tras un reinicio",
        injection=(
            f"reinicio rodante de los dos procesos de API mientras {profile.nodes} nodos envían a"
            " ritmo real"
        ),
        expected=(
            "ningún rate_limited en operación normal; el límite efectivo nunca baja del mínimo"
            " de NFR-CTR-02; retry_after_seconds entre 1 y 60 cuando sí limita"
        ),
    ) as run:
        seed = load_seed()
        fleet = platform.provision(profile)
        fleet.wait_until_ready()
        wall = profile.wall_seconds
        # Los dos reinicios, dentro del tramo en régimen y con la flota enviando (semilla).
        moments = sorted(run.random.uniform(0.2, 0.7) * wall for _ in API_PROCESSES)
        order = list(API_PROCESSES)
        run.random.shuffle(order)
        run.observe(
            load_seed=seed,
            profile=profile.describe(),
            restart_at_seconds=[round(moment, 1) for moment in moments],
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            started = WALL.monotonic()
            driving = pool.submit(
                run_profile,
                profile,
                fleet,
                seed=seed,
                dataset=sealed_dataset,
                cache=clip_cache,
                work=tmp_path,
                timeout=wall + DRIVER_MARGIN_SECONDS,
            )
            for name, moment in zip(order, moments, strict=True):
                time.sleep(max(0.0, moment - (WALL.monotonic() - started)))
                if driving.done():  # los nodos tienen que seguir enviando durante el reinicio
                    early = driving.result()
                    raise AssertionError(
                        f"los nodos terminaron antes del reinicio ({early.code}):\n"
                        + early.output[-4000:]
                    )
                platform.restart(name)
            driven = driving.result()
        assert driven.code == 0, driven.output
        assert driven.result is not None, driven.output
        analysis = analyse(driven.result)
        emitted = set(driven.result["journal"]["emitted"])
        # ``ledger_check`` solo usa ``stack`` del objetivo de carga, que esta plataforma también da.
        ledger = ledger_check(platform, fleet, emitted)  # type: ignore[arg-type]
        served = dict(platform.router.served)
        burst = _burst(platform, fleet)
        run.observe(
            restarts=platform.restarts,
            served_connections=served,
            emitted=analysis["emitted"],
            accepted=analysis["accepted"],
            lost=analysis["lost"],
            dead_letter=analysis["dead_letter"],
            rate_limited_by_operation=analysis["rate_limited_by_operation"],
            rate_limited_below_minimum=analysis["rate_limited_below_minimum"],
            rejection_codes=analysis["rejection_codes"],
            ledger=ledger,
            burst=burst,
            grant_minimum_per_minute=GRANT_MINIMUM,
        )

        assert [r["process"] for r in platform.restarts] == order
        assert all(r["exit_code"] == 0 for r in platform.restarts), platform.restarts
        assert all(served.get(name, 0) > 0 for name in API_PROCESSES), served
        # Operación normal: ningún rate_limited, nada perdido ni duplicado.
        assert analysis["rate_limited_by_operation"] == {}, analysis["rate_limited_by_operation"]
        assert analysis["rate_limited_below_minimum"] == 0
        assert analysis["emitted"] > 0
        assert analysis["accepted"] == analysis["emitted"], analysis
        assert analysis["lost"] == 0 and analysis["dead_letter"] == {}
        assert ledger["duplicated_source_keys"] == 0 and ledger["missing"] == 0, ledger
        # El límite efectivo, nunca por debajo del mínimo; cuando limita, retry_after de 1 a 60.
        assert burst["accepted_first_minute"] >= min(GRANT_MINIMUM, burst["requests_first_minute"])
        assert burst["limited"] > 0, "la ráfaga llega al límite: el caso «cuando sí limita»"
        assert all(1 <= value <= 60 for value in burst["retry_after_seconds"]), burst
        assert burst["retry_after_header_matches"], burst
        assert set(burst["statuses"]) <= {200, 429}, burst
