"""``export_u05_fixtures`` y ``openapi/u05-fixtures.json`` (TASK-462; BR-APP-02, PR-APP-01 y 12).

- ``--check`` termina en 0 con el archivo versionado y en 1, con la diferencia, si se edita a mano
  o si cambia la matriz sin regenerar; sin ``--check`` escribe exactamente lo que ``--check``
  espera.
- Una clave nueva de ``PermissionKey`` o una regla nueva de ``MATRIX`` sin regenerar se nombran en
  la diferencia (sobre una copia de prueba de la matriz: ``identity/authz/`` no se toca).
- Cada columna efectiva del archivo es exactamente lo que concede ``route_role`` en su contexto.
- Cada vector del corpus, reevaluado con ``FreeTextPolicy``, da su ``verdict`` (por igualdad) y
  hay al menos un vector rechazado por cada regla de la política.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.factories import session_hash, uuid7
from vigia_platform.identity.authz.authorize import route_role
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.ledger.free_text import FreeTextRejection, apply_base_policy
from vigia_platform.shared.api import export_u05_fixtures
from vigia_platform.shared.api.export_u05_fixtures import (
    ACCEPTED,
    DEFAULT_OUTPUT,
    FORMAT,
    SCHEMA_VERSION,
    EvaluationContext,
    FreeTextRoute,
    base_policy,
    differences,
    evaluate,
    free_text_routes,
    main,
    permission_differences,
    permissions_section,
    render,
)
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)

PROVIDER = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
CUSTOMER = uuid.UUID("00000000-0000-4000-8000-0000000c0570")
PROBE_KEY = "fixtures.probe"


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def routes() -> dict[str, FreeTextRoute]:
    return {route.route: route for route in free_text_routes()}


# --- Sincronía del archivo --------------------------------------------------------------------


def test_check_passes_on_the_committed_file(
    capsys: pytest.CaptureFixture[str], rendered: str
) -> None:
    assert main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == (
        f"{DEFAULT_OUTPUT} coincide con la matriz y la política actuales"
    )
    assert DEFAULT_OUTPUT.read_bytes() == rendered.encode("utf-8")
    assert b"\r\n" not in DEFAULT_OUTPUT.read_bytes()


def test_rendering_is_deterministic(rendered: str) -> None:
    assert render() == rendered


def test_the_file_is_ascii_with_its_header(committed: dict[str, Any]) -> None:
    DEFAULT_OUTPUT.read_bytes().decode("ascii")  # todo invisible va escrito como \uXXXX
    assert committed["format"] == FORMAT
    assert committed["schema_version"] == SCHEMA_VERSION
    assert set(committed["generated"]) == {"by", "check", "notice"}
    assert list(committed) == ["format", "schema_version", "generated", "permissions", "free_text"]


def test_writing_then_checking_round_trips(tmp_path: Path) -> None:
    output = tmp_path / "openapi" / "u05-fixtures.json"
    assert main(["--check", "--output", str(output)]) == 1  # falta
    assert main(["--output", str(output)]) == 0
    assert main(["--check", "--output", str(output)]) == 0


def test_a_hand_edited_rule_fails_the_check_with_the_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rendered: str
) -> None:
    document = json.loads(rendered)
    document["permissions"]["matrix"]["copasst"].append("findings.read")
    output = tmp_path / "u05-fixtures.json"
    output.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    assert main(["--check", "--output", str(output)]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert "diferencia: extra_in_file rule copasst findings.read" in errors
    assert f"--- {output} (versionado)" in errors
    assert f"+++ {output} (generado)" in errors


def test_a_hand_edited_verdict_fails_the_check_naming_the_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rendered: str
) -> None:
    document = json.loads(rendered)
    vector = next(v for v in document["free_text"]["vectors"] if v["verdict"] == "markup")
    vector["verdict"] = ACCEPTED
    output = tmp_path / "u05-fixtures.json"
    output.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    assert main(["--check", "--output", str(output)]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert f"diferencia: differs_in_file vector {vector['case']}" in errors


def test_a_file_that_is_not_json_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "u05-fixtures.json"
    output.write_text("{", encoding="utf-8")
    assert main(["--check", "--output", str(output)]) == 1
    assert "diferencia: invalid_file" in capsys.readouterr().err.splitlines()


# --- Matriz de permisos -----------------------------------------------------------------------


def _extended_keys() -> list[str]:
    """Copia de prueba de las claves de ``PermissionKey`` con una clave más."""
    return [*(key.value for key in PermissionKey), PROBE_KEY]


def test_the_committed_permissions_match_the_matrix(committed: dict[str, Any]) -> None:
    current = permissions_section(PermissionKey, MATRIX)
    assert permission_differences(committed["permissions"], current) == []
    assert committed["permissions"]["keys"] == sorted(key.value for key in PermissionKey)
    assert committed["permissions"]["roles"] == [role.value for role in Role]
    assert committed["permissions"]["matrix"] == {
        role.value: sorted(key.value for key in MATRIX[role]) for role in Role
    }


def test_a_new_key_without_regenerating_is_named(committed: dict[str, Any]) -> None:
    keys = _extended_keys()
    matrix = {role: {*MATRIX[role]} for role in Role}
    matrix[Role.COORDINATOR_SST].add(PROBE_KEY)  # type: ignore[arg-type]
    current = permissions_section(keys, matrix)
    assert permission_differences(committed["permissions"], current) == [
        ("missing_in_file", "key", PROBE_KEY),
        ("missing_in_file", "rule", "coordinator_sst", PROBE_KEY),
        ("missing_in_file", "effective", "customer_organization", "coordinator_sst", PROBE_KEY),
    ]
    # Con el archivo regenerado desde esa misma matriz, no queda diferencia.
    assert permission_differences(current, permissions_section(keys, matrix)) == []


def test_a_new_key_without_a_rule_is_named(committed: dict[str, Any]) -> None:
    current = permissions_section(_extended_keys(), MATRIX)
    assert permission_differences(committed["permissions"], current) == [
        ("missing_in_file", "key", PROBE_KEY),
    ]


def test_a_new_rule_without_regenerating_is_named(committed: dict[str, Any]) -> None:
    matrix = {role: {*MATRIX[role]} for role in Role}
    matrix[Role.COPASST].add(PermissionKey.FINDINGS_READ)
    matrix[Role.PLATFORM_OPERATOR].add(PermissionKey.FINDINGS_READ)
    current = permissions_section(PermissionKey, matrix)
    assert permission_differences(committed["permissions"], current) == [
        ("missing_in_file", "rule", "copasst", "findings.read"),
        ("missing_in_file", "rule", "platform_operator", "findings.read"),
        ("missing_in_file", "effective", "customer_organization", "copasst", "findings.read"),
        (
            "missing_in_file",
            "effective",
            "provider_organization",
            "platform_operator",
            "findings.read",
        ),
    ]


def test_a_changed_matrix_fails_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    matrix = {role: frozenset({*MATRIX[role], PROBE_KEY}) for role in Role}
    monkeypatch.setattr(export_u05_fixtures, "PermissionKey", _extended_keys())
    monkeypatch.setattr(export_u05_fixtures, "MATRIX", matrix)
    assert main(["--check"]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert f"diferencia: missing_in_file key {PROBE_KEY}" in errors
    assert f"diferencia: missing_in_file rule copasst {PROBE_KEY}" in errors


def _context(organization: uuid.UUID, role: Role, *, concession: bool) -> ScopeContext:
    """Contexto de sesión con una sola asignación de organización de ``role``."""
    concession_id = uuid.uuid4() if concession else None
    return _seal_scope_context(
        organization_id=organization,
        actor=Actor(
            kind=ActorKind.PROVIDER_USER if concession else ActorKind.USER,
            id=uuid.uuid4(),
            display_name_snapshot="Actor sintético",
            unit=ActorUnit.U02,
            concession_id=concession_id,
        ),
        origin=ContextOrigin.SESSION,
        allowed_scopes=[AllowedScope(ScopeLevel.ORGANIZATION, organization, role)],
        correlation_id=uuid7(),
        session_id_hash=session_hash(),
    )


_CONTEXTS = {
    EvaluationContext.CUSTOMER_ORGANIZATION: (CUSTOMER, False),
    EvaluationContext.PROVIDER_ORGANIZATION: (PROVIDER, False),
    EvaluationContext.CUSTOMER_UNDER_CONCESSION: (CUSTOMER, True),
}


@pytest.mark.parametrize("context", list(EvaluationContext), ids=str)
@pytest.mark.parametrize("role", list(Role), ids=str)
def test_each_effective_column_is_what_route_role_grants(
    committed: dict[str, Any], context: EvaluationContext, role: Role
) -> None:
    organization, concession = _CONTEXTS[context]
    scope_context = _context(organization, role, concession=concession)
    granted = sorted(
        key.value
        for key in PermissionKey
        if route_role(scope_context, key, provider_organization_id=PROVIDER) is role
    )
    assert committed["permissions"]["effective"][context.value][role.value] == granted


def test_the_platform_keys_are_the_prefixed_ones(committed: dict[str, Any]) -> None:
    permissions = committed["permissions"]
    assert permissions["platform_keys"] == sorted(
        key.value for key in PermissionKey if key.value.startswith(permissions["platform_prefix"])
    )
    assert permissions["platform_keys"] != []


# --- Texto libre ------------------------------------------------------------------------------


def test_the_routes_are_the_declared_free_text_paths(
    committed: dict[str, Any], routes: dict[str, FreeTextRoute]
) -> None:
    assert [route["route"] for route in committed["free_text"]["routes"]] == sorted(routes)
    for route in committed["free_text"]["routes"]:
        declared = routes[route["route"]]
        assert route == {
            "route": declared.route,
            "record_type": declared.record_type,
            "path": declared.path,
            "min_length": declared.min_length,
            "max_length": declared.max_length,
        }


def test_every_vector_reevaluates_to_its_verdict(
    committed: dict[str, Any], routes: dict[str, FreeTextRoute]
) -> None:
    policy = base_policy()
    vectors = committed["free_text"]["vectors"]
    assert len({vector["case"] for vector in vectors}) == len(vectors)
    for vector in vectors:
        route = routes[vector["route"]]
        verdict, normalized = evaluate(policy, route, vector["text"])
        assert vector["verdict"] == verdict, vector["case"]
        assert vector["normalized"] == normalized, vector["case"]
        # Y con la política base directamente, sin el registro.
        try:
            assert apply_base_policy(vector["text"], route.field) == vector["normalized"]
        except ValueError as rejection:
            assert getattr(rejection, "reason", None) == vector["verdict"], vector["case"]


def test_the_verdicts_are_the_closed_list(committed: dict[str, Any]) -> None:
    free_text = committed["free_text"]
    assert free_text["verdicts"] == [ACCEPTED, *(reason.value for reason in FreeTextRejection)]
    assert {vector["verdict"] for vector in free_text["vectors"]} <= set(free_text["verdicts"])


@pytest.mark.parametrize("reason", list(FreeTextRejection), ids=str)
def test_every_rule_has_a_rejected_vector(
    committed: dict[str, Any], reason: FreeTextRejection
) -> None:
    verdicts = [vector["verdict"] for vector in committed["free_text"]["vectors"]]
    assert verdicts.count(reason.value) >= 1


def test_every_route_has_its_length_boundaries(committed: dict[str, Any]) -> None:
    vectors = {vector["case"]: vector for vector in committed["free_text"]["vectors"]}
    for route in committed["free_text"]["routes"]:
        name = route["route"]
        assert vectors[f"length:max:{name}"]["verdict"] == ACCEPTED
        assert len(vectors[f"length:max:{name}"]["text"]) == route["max_length"]
        assert vectors[f"length:above-max:{name}"]["verdict"] == FreeTextRejection.TOO_LONG.value
        assert vectors[f"length:min:{name}"]["verdict"] == ACCEPTED
        assert vectors[f"length:below-min:{name}"]["verdict"] == FreeTextRejection.TOO_SHORT.value


def test_the_corpus_covers_nfc_and_the_a45_invisibles(committed: dict[str, Any]) -> None:
    vectors = {vector["case"]: vector for vector in committed["free_text"]["vectors"]}
    normalized = [
        v for v in vectors.values() if v["verdict"] == ACCEPTED and v["normalized"] != v["text"]
    ]
    assert normalized != []
    assert vectors["nfc-length:combining-at-max"]["verdict"] == ACCEPTED
    assert vectors["utf16-length:astral-at-max"]["verdict"] == ACCEPTED
    for code in (0x3164, 0xFFA0, 0x034F, 0xFE00, 0xFE0F, 0x2800):  # A-45, punto 1
        case = f"character:U+{code:04X}"
        assert vectors[case]["verdict"] == FreeTextRejection.INVISIBLE_CHARACTER.value, case
    for prefix, verdict in (("markup:", "markup"), ("plain:", ACCEPTED), ("nfc:", ACCEPTED)):
        cases = [v for case, v in vectors.items() if case.startswith(prefix)]
        assert cases != []
        assert {v["verdict"] for v in cases} == {verdict}, prefix


def test_differences_name_route_and_vector_changes(rendered: str) -> None:
    current = json.loads(rendered)
    document = json.loads(rendered)
    removed = document["free_text"]["routes"].pop(0)
    document["free_text"]["vectors"][0]["text"] += "x"
    document["free_text"]["seed"] = 0
    assert differences(document, current) == [
        ("field_differs", "free_text.seed"),
        ("missing_in_file", "route", removed["route"]),
        ("differs_in_file", "vector", current["free_text"]["vectors"][0]["case"]),
    ]
    assert differences(current, current) == []


def test_the_module_runs_as_a_script() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "vigia_platform.shared.api.export_u05_fixtures", "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
