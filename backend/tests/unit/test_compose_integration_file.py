"""Metapropiedades de ``compose.integration.yml`` y de los guiones de ``local/integration/``
(TASK-545, VIG-393; A-76 D3; A-89). Sin Docker: leen el archivo de composición como YAML.

La que importa es la superficie expuesta. ``AlbCertificateHeaders`` confía en las cabeceras
``X-Amzn-Mtls-*`` sin verificar la cadena: una petición directa a ``vigia-api`` con las cabeceras
públicas de un nodo devolvió 200 en la revisión de VIG-381. Por eso la composición debe cumplir,
siempre, que:

- solo ``vigia-mtls-edge`` publica puertos, y siempre en la IP de ``VIGIA_EDGE_BIND`` (obligatoria:
  sin ella el archivo no se interpreta), nunca en ``0.0.0.0``;
- ``vigia-api``, PostgreSQL y LocalStack viven solo en la red ``interna`` (``internal: true``);
- ningún servicio usa ``network_mode``, que se saltaría las dos reglas anteriores.

Además: las imágenes de PostgreSQL y LocalStack son las de ``docker-compose.yml`` (mismos digests),
ningún secreto va en el archivo, y el entorno de ``vigia-api`` lo acepta ``RuntimeConfig``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sys
import threading
import types
import uuid
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

from tests.integration.conftest import LOCALSTACK_IMAGE, POSTGRES_IMAGE
from vigia_platform.shared.runtime.config import NODE_CA_FILE_ENVIRONMENTS, RuntimeConfig

REPOSITORY: Final = Path(__file__).resolve().parents[3]
INTEGRATION: Final = REPOSITORY / "local" / "integration"
COMPOSE: Final = yaml.safe_load((REPOSITORY / "compose.integration.yml").read_text("utf-8"))
SERVICES: Final[dict[str, dict[str, Any]]] = COMPOSE["services"]
EDGE: Final = "vigia-mtls-edge"
INTERNAL_ONLY: Final = (
    "postgres",
    "localstack",
    "vigia-api-1",
    "vigia-api-2",
    "vigia-worker",
    "init",
    "vigia-migrate",
    "vigia-admin",
)
BIND_REFERENCE: Final = "${VIGIA_EDGE_BIND:?"


def load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- Superficie expuesta ------------------------------------------------------------------------


def test_only_the_edge_publishes_ports() -> None:
    publishing = {name for name, service in SERVICES.items() if service.get("ports")}
    assert publishing == {EDGE}


def test_edge_ports_are_bound_to_the_required_tailnet_address() -> None:
    ports = SERVICES[EDGE]["ports"]
    assert len(ports) == 2
    for port in ports:
        # `${VIGIA_EDGE_BIND:?mensaje}`: sin la variable, `docker compose` no interpreta el archivo.
        assert port.startswith(BIND_REFERENCE), port
        wide = "0." + "0.0.0"  # el comodín, sin escribirlo literal
        assert wide not in port and not port.startswith(("8443", "8444", ":")), port
    environment = SERVICES[EDGE]["environment"]
    assert environment["VIGIA_EDGE_PUBLISH_IP"].startswith(BIND_REFERENCE)


def test_no_service_escapes_the_network_rules() -> None:
    for name, service in SERVICES.items():
        assert "network_mode" not in service, name
        assert "expose" not in service, name
        assert "ports" not in service or name == EDGE, name
        assert not service.get("privileged"), name
    wide = "0." + "0.0.0"
    assert wide not in json.dumps({k: v.get("ports") for k, v in SERVICES.items()})


def test_internal_services_are_only_on_the_internal_network() -> None:
    networks = COMPOSE["networks"]
    assert networks["interna"]["internal"] is True
    assert not networks["publica"].get("internal", False)
    for name in INTERNAL_ONLY:
        assert list(SERVICES[name]["networks"]) == ["interna"], name
    assert set(SERVICES[EDGE]["networks"]) == {"interna", "publica"}


def test_the_public_network_has_no_other_member() -> None:
    members = {name for name, s in SERVICES.items() if "publica" in s.get("networks", [])}
    assert members == {EDGE}


def test_the_edge_reads_the_root_only_from_the_internal_localstack() -> None:
    environment = SERVICES[EDGE]["environment"]
    assert environment["VIGIA_EDGE_CLIENT_CA"] == "http://localstack:4566/vigia-edge/ca/root.pem"
    assert environment["VIGIA_EDGE_BACKENDS"] == "http://vigia-api-1:8000,http://vigia-api-2:8000"


def test_every_application_service_waits_for_the_provider_in_the_app_profile() -> None:
    # La API necesita el identificador de la proveedora, que existe tras el primer bootstrap.
    for name in ("vigia-api-1", "vigia-api-2", "vigia-worker", EDGE):
        assert SERVICES[name]["profiles"] == ["app"], name
    for name in ("vigia-migrate", "vigia-admin"):
        assert SERVICES[name]["profiles"] == ["tools"], name
    for name in ("init", "postgres", "localstack"):
        assert "profiles" not in SERVICES[name], name


# --- Imágenes y secretos ------------------------------------------------------------------------


def test_postgres_and_localstack_are_the_pinned_images_of_the_test_environment() -> None:
    reference = yaml.safe_load((REPOSITORY / "docker-compose.yml").read_text("utf-8"))["services"]
    assert SERVICES["postgres"]["image"] == reference["postgres"]["image"] == POSTGRES_IMAGE
    assert SERVICES["localstack"]["image"] == reference["localstack"]["image"] == LOCALSTACK_IMAGE
    for name in ("postgres", "localstack"):
        assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", SERVICES[name]["image"]), name


def test_the_localstack_services_match_the_test_environment() -> None:
    reference = yaml.safe_load((REPOSITORY / "docker-compose.yml").read_text("utf-8"))["services"]
    wanted = reference["localstack"]["environment"]
    mine = SERVICES["localstack"]["environment"]
    for key in ("SERVICES", "S3_SKIP_SIGNATURE_VALIDATION", "SKIP_SSL_CERT_DOWNLOAD"):
        assert mine[key] == wanted[key], key


def test_no_secret_value_in_the_compose_file() -> None:
    text = (REPOSITORY / "compose.integration.yml").read_text("utf-8")
    assert not re.search(r"(?i)^\s*[A-Z_]*(PASSWORD|SECRET_KEY|TOKEN)\s*:\s*\S", text, re.M), (
        "una contraseña o un token van en el volumen `estado`, nunca en el archivo"
    )
    assert SERVICES["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == "/state/postgres.pw"  # noqa: S105
    for name in ("vigia-api-1", "vigia-api-2", "vigia-worker", EDGE):
        mounts = " ".join(SERVICES[name]["volumes"])
        assert "estado:" not in mounts, f"{name} no debe montar los secretos"


def test_only_the_tools_can_write_the_node_authority_key() -> None:
    for name in ("vigia-api-1", "vigia-api-2", "vigia-worker"):
        assert "autoridad:/ca:ro" in SERVICES[name]["volumes"], name
    assert "autoridad:/ca" in SERVICES["vigia-admin"]["volumes"]


# --- Entorno de vigia-api -----------------------------------------------------------------------


def resolve(value: str) -> str:
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", value)


def runtime_environment() -> dict[str, str]:
    raw = SERVICES["vigia-api-1"]["environment"]
    environ = {key: resolve(str(value)) for key, value in raw.items()}
    environ["VIGIA_PROVIDER_ORGANIZATION_ID"] = str(uuid.UUID(int=7))
    return environ


def test_the_api_environment_is_accepted_by_runtime_config() -> None:
    config = RuntimeConfig.from_environ(runtime_environment())
    assert config.node_ca_backend == "file"
    assert config.node_ca_key_file == "/ca/node-ca.pem"
    assert config.aws_endpoint_url == "http://localstack:4566"


def test_the_file_authority_is_only_used_where_runtime_config_allows_it() -> None:
    environ = runtime_environment()
    assert environ["VIGIA_ENVIRONMENT"] in NODE_CA_FILE_ENVIRONMENTS
    assert environ["VIGIA_NODE_CA_BACKEND"] == "file"
    assert environ["VIGIA_NODE_CA_KEY_ARN"] == "file:node-ca"


def test_api_and_worker_share_one_environment() -> None:
    api = {
        k: v for k, v in SERVICES["vigia-api-1"]["environment"].items() if k != "OTEL_SERVICE_NAME"
    }
    worker = {
        k: v for k, v in SERVICES["vigia-worker"]["environment"].items() if k != "OTEL_SERVICE_NAME"
    }
    assert api == worker
    assert SERVICES["vigia-api-2"]["environment"] == SERVICES["vigia-api-1"]["environment"]


def test_the_forwarded_addresses_are_the_internal_subnet() -> None:
    subnet = COMPOSE["networks"]["interna"]["ipam"]["config"][0]["subnet"]
    environment = SERVICES["vigia-api-1"]["environment"]
    assert environment["VIGIA_FORWARDED_ALLOW_IPS"] == subnet


def test_the_secrets_key_is_the_same_in_localstack_and_the_runtime() -> None:
    assert (
        SERVICES["localstack"]["environment"]["VIGIA_KMS_KEY_ALIAS"]
        == SERVICES["vigia-api-1"]["environment"]["VIGIA_SECRETS_KEY_ARN"]
        == "alias/vigia-secrets"
    )


# --- edge_guard ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def guard() -> types.ModuleType:
    return load_module("vigia_edge_guard", INTEGRATION / "edge_guard.py")


@pytest.mark.parametrize("address", ["127.0.0.1", "100.64.12.7", "192.168.20.69", "fd7a:115c::1"])
def test_the_edge_guard_accepts_a_concrete_address(guard: types.ModuleType, address: str) -> None:
    assert guard.check_publish_ip({"VIGIA_EDGE_PUBLISH_IP": address}) is None


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "0.0.0.0",  # noqa: S104
        "::",
        "0:0:0:0:0:0:0:0",
        "tailscale0",
        "999.1.1.1",
        "224.0.0.1",
    ],
)
def test_the_edge_guard_refuses_a_wide_or_invalid_address(
    guard: types.ModuleType, address: str
) -> None:
    assert guard.check_publish_ip({"VIGIA_EDGE_PUBLISH_IP": address}) is not None


def test_the_edge_guard_refuses_a_missing_variable(guard: types.ModuleType) -> None:
    assert guard.check_publish_ip({}) is not None


# --- init_state ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def init_state() -> types.ModuleType:
    return load_module("vigia_init_state", INTEGRATION / "init_state.py")


def test_init_creates_private_secrets_once_and_never_replaces_them(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    first = init_state.ensure_state(state, owner=None)
    assert set(first) == {
        "postgres.pw",
        "app.pw",
        "migrate.pw",
        "kms-secrets.material",
        "kms-secrets.id",
        "enrollment-source.key",
    }
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    second = init_state.ensure_state(state, owner=None)
    assert second == []
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before
    for path in state.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600 if path.is_file() else True, path.name
    assert state.stat().st_mode & 0o777 == 0o700


def test_init_gives_the_kms_key_a_random_uuid_that_is_not_in_the_repository(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    init_state.ensure_state(tmp_path / "uno", owner=None)
    init_state.ensure_state(tmp_path / "dos", owner=None)
    ids = [(tmp_path / name / "kms-secrets.id").read_text().strip() for name in ("uno", "dos")]
    assert [str(uuid.UUID(value)) for value in ids] == ids
    assert ids[0] != ids[1]
    assert not any(value in (REPOSITORY / "compose.integration.yml").read_text() for value in ids)


def test_init_secrets_are_distinct_and_the_key_material_is_32_bytes(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    init_state.ensure_state(state, owner=None)
    passwords = {(state / n).read_text() for n in ("postgres.pw", "app.pw", "migrate.pw")}
    assert len(passwords) == 3
    for name in ("kms-secrets.material", "enrollment-source.key"):
        assert len(base64.b64decode((state / name).read_text().strip(), validate=True)) == 32


def test_init_pgpass_follows_the_postgres_password(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    init_state.ensure_state(state, owner=None)
    password = (state / "postgres.pw").read_text().strip()
    assert (state / "pgpass").read_text().strip() == f"postgres:5432:vigia:vigia:{password}"
    assert (state / "pgpass").stat().st_mode & 0o777 == 0o600


def test_init_never_replaces_an_existing_password_even_if_it_is_empty_of_neighbours(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "postgres.pw").write_text("la-de-siempre\n")
    (state / "postgres.pw").chmod(0o600)
    init_state.ensure_state(state, owner=None)
    assert (state / "postgres.pw").read_text() == "la-de-siempre\n"


def test_init_breach_list_falls_back_to_the_three_synthetic_entries(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    kind = init_state.ensure_breach_list(tmp_path, tmp_path / "ausente.txt", owner=None)
    lines = (tmp_path / "breach.txt").read_text().splitlines()
    assert kind == "sintetica"
    expected = sorted(
        hashlib.sha1(word.encode(), usedforsecurity=False).hexdigest().upper()
        for word in ("password", "123456", "qwerty")
    )
    assert lines == expected


def test_init_breach_list_takes_a_valid_real_list(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    digests = sorted(
        hashlib.sha1(w.encode(), usedforsecurity=False).hexdigest().upper() for w in "abc"
    )
    source = tmp_path / "real.txt"
    source.write_text("\n".join(digests) + "\n")
    assert init_state.ensure_breach_list(tmp_path, source, owner=None) == "real"
    assert (tmp_path / "breach.txt").read_text().splitlines() == digests


@pytest.mark.parametrize(
    "content", ["", "no-es-un-hash\n", "ab" * 20 + "\n", ("B" * 40 + "\n" + "A" * 40 + "\n")]
)
def test_init_breach_list_refuses_an_invalid_real_list(
    init_state: types.ModuleType, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "mala.txt"
    source.write_text(content)
    with pytest.raises(SystemExit):
        init_state.ensure_breach_list(tmp_path, source, owner=None)
    assert not (tmp_path / "breach.txt").exists()


# --- state.py de LocalStack ---------------------------------------------------------------------


class FakeManager:
    """Secrets Manager mínimo: guarda lo que recibe y lanza ``ResourceExistsException``."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.existing = existing or set()

    def create_secret(self, **arguments: Any) -> None:
        from botocore.exceptions import ClientError

        if arguments["Name"] in self.existing:
            raise ClientError({"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
        self.created.append(arguments)

    def put_secret_value(self, **arguments: Any) -> None:
        self.updated.append(arguments)


@pytest.fixture(scope="module")
def state_module() -> types.ModuleType:
    return load_module("vigia_localstack_state", INTEGRATION / "localstack" / "state.py")


ARN: Final = (
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:"
    "vigia/integration/signing/catalog-1-AbCdEf"
)


def test_a_restored_secret_keeps_its_arn_suffix_and_version(state_module: types.ModuleType) -> None:
    manager = FakeManager()
    version = str(uuid.UUID(int=5))
    state_module._put_secret(
        manager,
        {
            "name": "vigia/integration/signing/catalog-1",
            "arn": ARN,
            "version_id": version,
            "string": "x",
        },
    )
    (call,) = manager.created
    assert call["Tags"] == [{"Key": "_custom_id_", "Value": "AbCdEf"}]
    assert call["ClientRequestToken"] == version
    assert call["SecretString"] == "x"


def test_an_existing_secret_is_updated_not_recreated(state_module: types.ModuleType) -> None:
    manager = FakeManager({"nombre"})
    state_module._put_secret(
        manager, {"name": "nombre", "arn": ARN, "version_id": "v", "string": "nuevo"}
    )
    assert manager.created == []
    assert manager.updated == [{"SecretId": "nombre", "SecretString": "nuevo"}]


def test_a_binary_secret_round_trips(state_module: types.ModuleType) -> None:
    manager = FakeManager()
    payload = bytes(range(32))
    state_module._put_secret(
        manager, {"name": "bin", "binary": base64.b64encode(payload).decode(), "arn": None}
    )
    (call,) = manager.created
    assert call["SecretBinary"] == payload
    assert "Tags" not in call


def test_save_writes_nothing_without_a_finished_restore(
    state_module: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_module, "MARKER", tmp_path / "no-hay-marcador")
    monkeypatch.setattr(state_module, "SNAPSHOT", tmp_path / "copia.json")
    (tmp_path / "copia.json").write_text("buena")
    assert state_module.save() == 3
    assert (tmp_path / "copia.json").read_text() == "buena"


def test_the_snapshot_is_atomic_private_and_not_rewritten_when_equal(
    state_module: types.ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "copia.json"
    assert state_module.write_atomically(target, "uno") is True
    assert target.stat().st_mode & 0o777 == 0o600
    mtime = target.stat().st_mtime_ns
    assert state_module.write_atomically(target, "uno") is False
    assert target.stat().st_mtime_ns == mtime
    assert state_module.write_atomically(target, "dos") is True
    assert target.read_text() == "dos"
    assert [p.name for p in tmp_path.iterdir()] == ["copia.json"]


# --- Garantías atómicas, con prueba concurrente -----------------------------------------------


def test_concurrent_inits_create_each_secret_once_and_never_expose_a_partial_file(
    init_state: types.ModuleType, tmp_path: Path
) -> None:
    """Dos ``init`` a la vez: gana uno por archivo y nadie lee un secreto a medio escribir."""
    state = tmp_path / "state"
    workers = 8
    barrier = threading.Barrier(workers + 1)
    created: list[list[str]] = []
    problems: list[str] = []
    stop = threading.Event()

    def initialise() -> None:
        barrier.wait()
        created.append(init_state.ensure_state(state, owner=None))

    def watch() -> None:
        barrier.wait()
        while not stop.is_set():
            for name in ("postgres.pw", "app.pw", "migrate.pw", "kms-secrets.material", "pgpass"):
                try:
                    text = (state / name).read_text()
                except FileNotFoundError:
                    continue
                if not text.endswith(chr(10)) or len(text.strip()) < 20:
                    problems.append(f"{name} se vio a medias: {len(text)} bytes")

    threads = [threading.Thread(target=initialise) for _ in range(workers)]
    watcher = threading.Thread(target=watch)
    for thread in (*threads, watcher):
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    stop.set()
    watcher.join(timeout=30)
    assert not problems, problems[:3]
    flat = sorted(name for names in created for name in names)
    assert flat == sorted(
        [
            "postgres.pw",
            "app.pw",
            "migrate.pw",
            "kms-secrets.material",
            "kms-secrets.id",
            "enrollment-source.key",
        ]
    ), "cada secreto lo crea un único init"
    assert sorted(path.name for path in state.iterdir()) == sorted(
        [
            "postgres.pw",
            "app.pw",
            "migrate.pw",
            "kms-secrets.material",
            "kms-secrets.id",
            "enrollment-source.key",
            "pgpass",
        ]
    ), "no quedan temporales"
    password = (state / "postgres.pw").read_text().strip()
    assert (state / "pgpass").read_text().strip() == f"postgres:5432:vigia:vigia:{password}"


def test_concurrent_snapshots_are_never_torn(
    state_module: types.ModuleType, tmp_path: Path
) -> None:
    """La copia periódica y la forzada por ``bootstrap.sh`` pueden coincidir: gana una, entera."""
    target = tmp_path / "copia.json"
    contents = [json.dumps({"escritor": index, "relleno": "x" * 4000}) for index in range(6)]
    barrier = threading.Barrier(len(contents) + 1)
    stop = threading.Event()
    seen: list[str] = []

    def write(text: str) -> None:
        barrier.wait()
        for _ in range(40):
            state_module.write_atomically(target, text)

    def read() -> None:
        barrier.wait()
        while not stop.is_set():
            try:
                seen.append(target.read_text())
            except FileNotFoundError:
                continue

    writers = [threading.Thread(target=write, args=(text,)) for text in contents]
    reader = threading.Thread(target=read)
    for thread in (*writers, reader):
        thread.start()
    for thread in writers:
        thread.join(timeout=60)
    stop.set()
    reader.join(timeout=30)
    assert seen, "el lector no vio nunca la copia"
    assert all(text in contents for text in seen), "se leyó una copia a medias"
    assert target.read_text() in contents
    assert [path.name for path in tmp_path.iterdir()] == ["copia.json"], "quedó un temporal"
    assert target.stat().st_mode & 0o777 == 0o600
