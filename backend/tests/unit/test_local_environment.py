"""Comprobaciones estáticas del entorno local (TASK-103, LC-NUC-36, PAT-NUC-MAN-07).

Sin Docker: leen ``docker-compose.yml``, los ``Dockerfile`` de ``local/`` y el ``Makefile`` de la
raíz del repositorio y fijan lo que no debe cambiar sin revisión:

- los tres servicios (PostgreSQL 16, LocalStack y el colector de OpenTelemetry), sin MinIO (AGPL);
- toda imagen de origen fijada por digest, ninguna por ``latest`` (criterio 3);
- las imágenes de testcontainers son las mismas que las de compose (el mismo entorno en el PC y en
  la canalización);
- los puertos publicados solo en ``127.0.0.1``;
- los objetivos ``test``, ``run``, ``worker``, ``migrate`` y ``admin`` del ``Makefile`` y sus
  equivalentes con ``uv run`` en el README.

``docker-compose.yml`` se lee línea a línea (el repositorio no declara un analizador de YAML):
la prueba ``test_compose_parser_sees_every_service`` protege ese lector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.integration.conftest import LOCALSTACK_IMAGE, POSTGRES_IMAGE

REPOSITORY = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPOSITORY / "docker-compose.yml"
MAKEFILE = REPOSITORY / "Makefile"
README = REPOSITORY / "README.md"
LOCAL_DIRECTORY = REPOSITORY / "local"

EXPECTED_SERVICES = {"postgres", "localstack", "otel-collector"}
MAKE_TARGETS = ("test", "run", "worker", "migrate", "admin")

PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
"""``nombre:etiqueta@sha256:<64 hex>``: etiqueta legible y digest que manda."""


@dataclass
class ComposeService:
    name: str
    image: str | None = None
    build: bool = False
    ports: list[str] = field(default_factory=list)


def _compose_services(text: str) -> dict[str, ComposeService]:
    """Servicios de ``docker-compose.yml`` con su imagen, si se construye y sus puertos."""
    services: dict[str, ComposeService] = {}
    in_services = False
    current: ComposeService | None = None
    in_ports = False
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        if not line.startswith(" "):
            in_services = line == "services:"
            current = None
            continue
        if not in_services:
            continue
        if match := re.fullmatch(r"  ([a-z0-9][a-z0-9_-]*):", line):
            current = services.setdefault(match[1], ComposeService(match[1]))
            in_ports = False
            continue
        if current is None:
            continue
        if match := re.fullmatch(r"    image:\s*(\S+)", line):
            current.image = match[1]
        elif re.fullmatch(r"    build:.*", line):
            current.build = True
        if re.fullmatch(r"    ports:", line):
            in_ports = True
            continue
        if in_ports and (match := re.fullmatch(r"      - \"?([^\"]+)\"?", line)):
            current.ports.append(match[1])
        elif re.match(r"    \S", line):
            in_ports = False
    return services


def _dockerfile_images(path: Path) -> list[str]:
    return [
        match[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", line))
    ]


@pytest.fixture(scope="module")
def services() -> dict[str, ComposeService]:
    return _compose_services(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_compose_parser_sees_every_service() -> None:
    sample = (
        "name: x\n"
        "services:\n"
        "  a:\n"
        "    # comentario\n"
        "    image: a:1@sha256:" + "0" * 64 + "\n"
        "    ports:\n"
        '      - "127.0.0.1:1:1"\n'
        "    healthcheck:\n"
        "      test:\n"
        "        - CMD\n"
        "  b:\n"
        "    build:\n"
        "      context: ./b\n"
        "    image: b:local\n"
        "volumes:\n"
        "  data:\n"
    )
    parsed = _compose_services(sample)
    assert set(parsed) == {"a", "b"}
    assert parsed["a"].image == "a:1@sha256:" + "0" * 64
    assert parsed["a"].ports == ["127.0.0.1:1:1"]
    assert not parsed["a"].build
    assert parsed["b"].build
    assert parsed["b"].ports == []


def test_compose_has_the_three_services(services: dict[str, ComposeService]) -> None:
    assert set(services) == EXPECTED_SERVICES


def test_no_minio_anywhere() -> None:
    """MinIO es AGPL: fuera de la lista de licencias permitidas (tech-stack-decisions.md §2).

    ``docker-compose.yml`` solo lo nombra en el comentario que explica por qué no está.
    """
    compose_code = [
        line
        for line in COMPOSE_FILE.read_text(encoding="utf-8").lower().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("minio" in line for line in compose_code)
    for path in [MAKEFILE, *LOCAL_DIRECTORY.rglob("*")]:
        if path.is_file():
            assert "minio" not in path.read_text(encoding="utf-8").lower(), path


def test_every_source_image_is_pinned_by_digest(services: dict[str, ComposeService]) -> None:
    """Criterio 3: ninguna imagen por ``latest``; toda imagen de origen fijada por digest."""
    source_images: list[str] = []
    for service in services.values():
        assert service.image is not None, f"{service.name} sin image:"
        if service.build:
            # La imagen construida se nombra con la versión del colector, nunca con `latest`.
            assert re.fullmatch(r"vigia-local/[a-z0-9-]+:\d+\.\d+\.\d+", service.image)
        else:
            source_images.append(service.image)
    dockerfiles = sorted(LOCAL_DIRECTORY.rglob("Dockerfile"))
    assert dockerfiles, "local/ debería tener el Dockerfile del colector"
    for dockerfile in dockerfiles:
        images = _dockerfile_images(dockerfile)
        assert images, dockerfile
        source_images.extend(images)
    for image in source_images:
        assert PINNED_IMAGE.fullmatch(image), f"imagen sin digest: {image}"
        assert ":latest" not in image


@pytest.mark.parametrize(
    ("image", "pinned"),
    [
        ("postgres:16@sha256:" + "a" * 64, True),
        ("postgres:latest@sha256:" + "a" * 64, True),  # la regla de `latest` va aparte
        ("postgres:16", False),
        ("postgres@sha256:" + "a" * 64, False),
        ("postgres:16@sha256:" + "a" * 63, False),
        ("postgres:16@sha256:" + "A" * 64, False),
    ],
)
def test_pinned_image_pattern_edges(image: str, pinned: bool) -> None:
    assert bool(PINNED_IMAGE.fullmatch(image)) is pinned


def test_testcontainers_use_the_compose_images(services: dict[str, ComposeService]) -> None:
    assert services["postgres"].image == POSTGRES_IMAGE
    assert services["localstack"].image == LOCALSTACK_IMAGE
    assert POSTGRES_IMAGE.startswith("postgres:16@")
    assert LOCALSTACK_IMAGE.startswith("localstack/localstack:")


def test_ports_only_listen_on_localhost(services: dict[str, ComposeService]) -> None:
    for service in services.values():
        assert service.ports, f"{service.name} no publica puertos"
        for port in service.ports:
            assert port.startswith("127.0.0.1:"), f"{service.name}: {port}"


def test_makefile_has_the_standard_targets() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in MAKE_TARGETS:
        assert re.search(rf"^{target}:", text, flags=re.MULTILINE), target
        assert re.search(rf"^\.PHONY:.*\b{target}\b", text, flags=re.MULTILINE), target


def test_readme_documents_uv_run_equivalents_for_windows() -> None:
    text = README.read_text(encoding="utf-8")
    for command in (
        "docker compose up -d",
        "docker compose down -v",
        "uv run pytest -q --hypothesis-profile=ci",
        "uv run uvicorn vigia_platform.shared.api.app:create_app --factory",
        "uv run python -m vigia_platform.shared.worker.main",
        "uv run alembic upgrade head",
        "uv run vigia-admin",
        "VIGIA_TEST_USE_COMPOSE",
    ):
        assert command in text, command
