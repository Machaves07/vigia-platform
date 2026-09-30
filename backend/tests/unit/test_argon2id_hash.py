"""Hash Argon2id con parámetros versionados y recálculo (LC-NUC-01, NFR-NUC-26, TASK-122).

- ``hash`` usa Argon2id con 64 MB, 3 iteraciones y paralelismo 2 (versión 1) y lo hace en el
  pool de CPU, nunca en el bucle de eventos (PAT-NUC-REN-05).
- ``verify`` devuelve ``(ok, needs_rehash)``: con un hash de parámetros antiguos y la
  contraseña correcta, ``needs_rehash`` es ``True``; con la contraseña incorrecta, nunca.
- Un hash ilegible, de otro algoritmo o de otro tipo nunca verifica y nunca lanza.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher, Type
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from tests.hibp_service import FakeRangeService, checker, local_list, metrics_with_reader
from vigia_platform.identity.auth import passwords
from vigia_platform.identity.auth.passwords import (
    CURRENT_VERSION,
    HASH_VERSIONS,
    PasswordService,
    VerifyResult,
    hash_password,
    verify_password,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CPU_POOL_THREAD_PREFIX, CpuPool
from vigia_platform.shared.observability.metrics import MetricName

PLAINTEXT_SAMPLE = "clave-sintetica-larga-01"
OTHER_SAMPLE = "clave-sintetica-larga-02"
START = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.fixture(scope="module")
def current_hash() -> str:
    return hash_password(PLAINTEXT_SAMPLE).encoded


def _legacy(**overrides: int) -> str:
    """Hash Argon2id de ``PLAINTEXT_SAMPLE`` con parámetros distintos de los vigentes."""
    params = {
        "time_cost": 3,
        "memory_cost": 64 * 1024,
        "parallelism": 2,
        "hash_len": 32,
        "salt_len": 16,
    }
    params.update(overrides)
    hasher = PasswordHasher(
        time_cost=params["time_cost"],
        memory_cost=params["memory_cost"],
        parallelism=params["parallelism"],
        hash_len=params["hash_len"],
        salt_len=params["salt_len"],
        type=Type.ID,
    )
    return hasher.hash(PLAINTEXT_SAMPLE)


def test_current_version_has_the_design_parameters(current_hash: str) -> None:
    params = HASH_VERSIONS[CURRENT_VERSION]
    assert (params.memory_kib, params.iterations, params.parallelism) == (65536, 3, 2)
    assert current_hash.startswith("$argon2id$v=19$m=65536,t=3,p=2$")
    assert hash_password(PLAINTEXT_SAMPLE).algorithm_version == CURRENT_VERSION


def test_round_trip_verifies_without_rehash(current_hash: str) -> None:
    assert verify_password(PLAINTEXT_SAMPLE, current_hash) == VerifyResult(
        ok=True, needs_rehash=False
    )
    ok, needs_rehash = verify_password(PLAINTEXT_SAMPLE, current_hash)  # se desempaqueta como tupla
    assert (ok, needs_rehash) == (True, False)


def test_salts_differ_between_hashes() -> None:
    assert hash_password(PLAINTEXT_SAMPLE).encoded != hash_password(PLAINTEXT_SAMPLE).encoded


def test_wrong_password_never_verifies(current_hash: str) -> None:
    assert verify_password(OTHER_SAMPLE, current_hash) == VerifyResult(ok=False, needs_rehash=False)
    assert verify_password("", current_hash) == VerifyResult(ok=False, needs_rehash=False)
    assert verify_password(PLAINTEXT_SAMPLE + " ", current_hash) == VerifyResult(False, False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_cost": 19 * 1024, "time_cost": 2, "parallelism": 1},  # parámetros antiguos
        {"memory_cost": 32 * 1024},
        {"time_cost": 2},
        {"parallelism": 1},
        {"hash_len": 16},
        {"salt_len": 8},
        {"time_cost": 4},  # también si los antiguos eran más altos: se vuelve a los vigentes
    ],
    ids=["antiguos", "memoria", "iteraciones", "paralelismo", "hash_len", "salt_len", "mayores"],
)
def test_old_parameters_need_rehash_only_when_the_password_is_right(
    overrides: dict[str, int],
) -> None:
    legacy = _legacy(**overrides)
    assert verify_password(PLAINTEXT_SAMPLE, legacy) == VerifyResult(ok=True, needs_rehash=True)
    assert verify_password(OTHER_SAMPLE, legacy) == VerifyResult(ok=False, needs_rehash=False)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "texto",
        "$argon2id$",
        "$argon2id$v=19$m=65536,t=3,p=2$",
        "$argon2id$v=19$m=65536,t=3,p=2$c2FsdA$!!!",
        "$argon2id$v=19$m=0,t=0,p=0$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA",
        "$2b$12$abcdefghijklmnopqrstuuJ6.5WmZLu2Fm1NL1Lv2jrmfB9jl8Ypm",  # bcrypt
    ],
    ids=["vacio", "texto", "prefijo", "sin-sal", "base64", "parametros-cero", "bcrypt"],
)
def test_unreadable_hashes_never_verify_nor_raise(encoded: str) -> None:
    assert verify_password(PLAINTEXT_SAMPLE, encoded) == VerifyResult(ok=False, needs_rehash=False)


def test_other_argon2_types_are_not_accepted() -> None:
    for kind in (Type.I, Type.D):
        other = PasswordHasher(type=kind).hash(PLAINTEXT_SAMPLE)
        assert verify_password(PLAINTEXT_SAMPLE, other) == VerifyResult(
            ok=False, needs_rehash=False
        )


def test_non_string_inputs_never_verify(current_hash: str) -> None:
    assert verify_password(PLAINTEXT_SAMPLE.encode(), current_hash) == VerifyResult(False, False)  # type: ignore[arg-type]
    assert verify_password(PLAINTEXT_SAMPLE, current_hash.encode()) == VerifyResult(False, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        hash_password(None)  # type: ignore[arg-type]


def test_unicode_and_lone_surrogates_round_trip() -> None:
    for secret in ("contraseña-ñandú-🙂", "clave\ud800suelta", chr(0x200B) * 8, "a" * 128):
        encoded = hash_password(secret).encoded
        assert verify_password(secret, encoded) == VerifyResult(ok=True, needs_rehash=False)
    # NFC y NFD son contraseñas distintas: el hash no normaliza.
    nfc = hash_password("contraseña").encoded
    assert verify_password("contrasen" + chr(0x0303) + "a", nfc).ok is False


def test_hash_repr_does_not_show_the_encoded_value() -> None:
    value = hash_password(PLAINTEXT_SAMPLE)
    assert value.encoded not in repr(value)
    assert "algorithm_version=1" in repr(value)


@pytest.fixture
def pool_and_reader() -> Iterator[tuple[CpuPool, InMemoryMetricReader]]:
    metrics, reader = metrics_with_reader()
    pool = CpuPool(SystemClock(), max_workers=2, metrics=metrics)
    yield pool, reader
    pool.shutdown()


@pytest.mark.asyncio
async def test_service_hashes_and_verifies_in_the_cpu_pool(
    pool_and_reader: tuple[CpuPool, InMemoryMetricReader], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, reader = pool_and_reader
    threads: list[str] = []
    real_hash, real_verify = passwords.hash_password, passwords.verify_password

    def spy_hash(secret: str) -> passwords.PasswordHash:
        threads.append(threading.current_thread().name)
        return real_hash(secret)

    def spy_verify(secret: str, encoded: str) -> VerifyResult:
        threads.append(threading.current_thread().name)
        return real_verify(secret, encoded)

    monkeypatch.setattr(passwords, "hash_password", spy_hash)
    monkeypatch.setattr(passwords, "verify_password", spy_verify)
    breach_checker = checker(FakeRangeService(), local_list(["señuelo-señuelo"]))
    service = PasswordService(breach_checker, pool)

    stored = await service.hash(PLAINTEXT_SAMPLE)
    assert await service.verify(PLAINTEXT_SAMPLE, stored.encoded) == (True, False)
    assert await service.verify(OTHER_SAMPLE, stored.encoded) == (False, False)
    assert await service.verify(PLAINTEXT_SAMPLE, _legacy(time_cost=2)) == (True, True)
    await breach_checker.aclose()

    assert len(threads) == 4
    assert all(name.startswith(CPU_POOL_THREAD_PREFIX) for name in threads)
    assert threading.main_thread().name not in threads
    data = reader.get_metrics_data()
    assert data is not None
    waits = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == MetricName.CPU_POOL_WAIT_MS.value
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    ]
    assert sum(point.count for point in waits) == 4


@pytest.mark.asyncio
async def test_hashing_does_not_block_the_event_loop(
    pool_and_reader: tuple[CpuPool, InMemoryMetricReader],
) -> None:
    """Mientras Argon2id calcula (≈100 ms), el bucle sigue atendiendo otras tareas."""
    pool, _ = pool_and_reader
    breach_checker = checker(FakeRangeService(), local_list(["señuelo-señuelo"]))
    service = PasswordService(breach_checker, pool)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await service.hash(PLAINTEXT_SAMPLE)
    task.cancel()
    await breach_checker.aclose()
    assert ticks >= 3
