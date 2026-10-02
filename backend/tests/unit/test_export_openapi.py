"""``export_openapi`` y ``backend/openapi/app.yaml`` (TASK-133; NFR-NUC-52).

- ``--check`` termina en 0 con el archivo versionado y en 1 si una ruta cambia sin regenerar (o si
  el archivo falta); sin ``--check`` escribe exactamente lo que ``--check`` espera.
- El YAML emitido se lee (``yaml.safe_load``) como el mismo documento que ``app.openapi()``,
  también para documentos JSON arbitrarios con claves y textos difíciles.
- La especificación documenta ``ApiErrorBody`` como respuesta de error de toda operación, sin el
  422 propio de FastAPI, y no publica la salud profunda.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.shared.api import app as app_module
from vigia_platform.shared.api import export_openapi
from vigia_platform.shared.api.app import UnitRegistration, build_openapi_app, platform_units
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.export_openapi import DEFAULT_OUTPUT, main, render, to_yaml


def test_check_passes_on_the_committed_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0
    assert "coincide" in capsys.readouterr().out
    assert DEFAULT_OUTPUT.read_bytes() == render().encode("utf-8")
    assert b"\r\n" not in DEFAULT_OUTPUT.read_bytes()


def _extra_route_units() -> tuple[UnitRegistration, ...]:
    router = APIRouter()

    # Una ruta nueva cualquiera (la de las claves públicas ya existe desde TASK-137).
    @router.get("/generated/extra", dependencies=[requires("hierarchy.read")])
    async def extra() -> dict[str, str]:
        return {}

    return (*platform_units(), UnitRegistration("prueba", routers=(router,)))


def test_changing_a_route_without_regenerating_fails_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app_module, "platform_units", _extra_route_units)
    assert main(["--check"]) == 1
    assert "no coincide con las rutas actuales" in capsys.readouterr().err


def test_writing_then_checking_round_trips(tmp_path: Path) -> None:
    output = tmp_path / "openapi" / "app.yaml"
    assert main(["--check", "--output", str(output)]) == 1  # falta
    assert main(["--output", str(output)]) == 0
    assert main(["--check", "--output", str(output)]) == 0
    output.write_bytes(output.read_bytes().replace(b"live", b"vivo"))
    assert main(["--check", "--output", str(output)]) == 1


def test_the_yaml_reads_back_as_the_specification() -> None:
    spec = build_openapi_app().openapi()
    assert yaml.safe_load(DEFAULT_OUTPUT.read_text(encoding="utf-8")) == spec
    assert "/health/ready" not in spec["paths"]
    operation = spec["paths"]["/health/live"]["get"]
    assert "422" not in operation["responses"]
    assert operation["responses"]["default"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorBody"
    }
    schemas = spec["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert set(schemas["ApiErrorCode"]["enum"]) >= {"privacy_notice_required", "not_found"}
    assert "csrf_rejected" not in schemas["ApiErrorCode"]["enum"]


json_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**53), 2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
    st.sampled_from(["", "null", "true", "- a", "a: b", "#x", "'", '"', "\\", "\t", "01", "~"]),
)
json_documents = st.recursive(
    json_leaves,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(string.printable, max_size=8), children, max_size=4),
    ),
    max_leaves=20,
)


@given(document=st.dictionaries(st.text(max_size=10), json_documents, max_size=5))
def test_any_json_document_reads_back_unchanged(document: dict[str, Any]) -> None:
    assert yaml.safe_load(to_yaml(document)) == (document or None)


def test_values_that_are_not_json_are_rejected() -> None:
    with pytest.raises(TypeError):
        to_yaml({"a": object()})


def test_the_module_runs_as_a_script() -> None:
    assert export_openapi.__name__ == "vigia_platform.shared.api.export_openapi"
    assert callable(export_openapi.main)
