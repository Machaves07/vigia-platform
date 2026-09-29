"""Formato del registro JSON (NFR-NUC-17) y bordes de la política de redacción."""

from __future__ import annotations

import enum
import io
import json
import logging
import re
import uuid
from collections.abc import Iterator

import pytest

from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import (
    CONTEXT_FIELDS,
    MAX_MESSAGE_CHARS,
    JsonFormatter,
    configure_logging,
    get_logger,
    log_context,
)

ORG = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
CORRELATION = "0190a1b2-c3d4-7e5f-8a9b-000000000001"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class RecordKind(enum.StrEnum):
    FINDING = "finding"


@pytest.fixture
def lines() -> Iterator[list[dict[str, object]]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    captured: list[dict[str, object]] = []
    try:
        yield captured
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    captured.extend(json.loads(line) for line in stream.getvalue().splitlines())


def _emit(lines: list[dict[str, object]], action: object) -> list[dict[str, object]]:
    return lines


def test_line_has_fixed_fields_then_context_fields_in_order() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        with log_context(correlation_id=CORRELATION, organization_id=ORG):
            get_logger("ledger.application").info(
                "registro escrito",
                route="unregistered",
                status=201,
                duration_ms=12.34567,
                actor_id=str(ORG),
                record_type=RecordKind.FINDING,
            )
    finally:
        root.removeHandler(handler)
    line = json.loads(stream.getvalue())
    assert list(line)[:4] == ["timestamp", "level", "component", "message"]
    assert TIMESTAMP.fullmatch(line["timestamp"])
    assert line["level"] == "INFO"
    assert line["component"] == "ledger.application"
    assert line["message"] == "registro escrito"
    context_keys = [key for key in line if key in CONTEXT_FIELDS]
    assert context_keys == [key for key in CONTEXT_FIELDS if key in line]
    assert line["correlation_id"] == CORRELATION
    assert line["organization_id"] == str(ORG)
    assert line["route"] == redaction.REDACTED  # Plantilla no registrada.
    assert line["status"] == 201
    assert line["duration_ms"] == 12.346
    assert line["record_type"] == "finding"
    assert "redacted_fields" not in line


def _format(record: logging.LogRecord) -> dict[str, object]:
    result: dict[str, object] = json.loads(JsonFormatter().format(record))
    return result


def _record(msg: object, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise KeyError("dato-sensible")
        except KeyError:
            import sys

            exc_info = sys.exc_info()
    return logging.LogRecord("tercero.biblioteca", logging.WARNING, "f.py", 1, msg, args, exc_info)


def test_percent_arguments_keep_numbers_uuids_and_enums_only() -> None:
    line = _format(_record("%s %s %s %s %s", 7, 2.5, ORG, RecordKind.FINDING, "texto libre"))
    assert line["message"] == f"7 2.5 {ORG} finding {redaction.REDACTED}"


def test_mapping_arguments_and_broken_templates_do_not_fail() -> None:
    record = logging.LogRecord("x", logging.INFO, "f.py", 1, "n=%(n)s", ({"n": 3},), None)
    assert _format(record)["message"] == "n=3"
    assert _format(_record("sin %s marcador %d", "a"))["message"] == "sin %s marcador %d"


def test_exception_logs_type_only() -> None:
    line = _format(_record("fallo", exc=True))
    assert line["exception_type"] == "KeyError"
    assert "dato-sensible" not in json.dumps(line)


def test_non_string_message_and_long_message() -> None:
    assert _format(_record(ValueError("dato")))["message"] == redaction.REDACTED
    long = _format(_record("a " * 600))["message"]
    assert isinstance(long, str) and len(long) == MAX_MESSAGE_CHARS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (-1, redaction.REDACTED),
        (float("nan"), redaction.REDACTED),
        (True, redaction.REDACTED),
    ],
)
def test_duration_ms_bounds(value: object, expected: object) -> None:
    record = _record("medida")
    record.duration_ms = value
    assert _format(record)["duration_ms"] == expected


def test_unknown_fields_are_counted_not_named() -> None:
    record = _record("evento")
    record.session_id = "abc"
    record.password = "x"  # noqa: S105 — campo desconocido de la prueba.
    line = _format(record)
    assert line["redacted_fields"] == 2
    assert "session_id" not in line and "password" not in line


def test_configure_logging_replaces_handlers_and_quiets_libraries() -> None:
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        stream = io.StringIO()
        configure_logging("debug", stream=stream)
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("opentelemetry").level == logging.ERROR
        get_logger("pruebas").debug("hola")
        assert json.loads(stream.getvalue())["message"] == "hola"
        configure_logging("no-existe", stream=stream)
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


# --- Política de atributos -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("status", 99, None),
        ("status", 100, 100),
        ("status", 599, 599),
        ("status", 600, None),
        ("status", True, None),
        ("status", "200", None),
        ("organization_id", ORG, str(ORG)),
        ("organization_id", str(ORG), str(ORG)),
        ("organization_id", str(ORG).upper(), redaction.OTHER),
        ("organization_id", "", redaction.OTHER),
        ("task", "evidence_sample", "evidence_sample"),
        ("task", "EVIDENCE_SAMPLE", redaction.OTHER),
        ("result", RecordKind.FINDING, "finding"),
        ("result", "finding", redaction.OTHER),
        ("exception.escaped", True, True),
        ("exception.escaped", 1, None),
        ("session_id", "cualquiera", None),
        ("http.target", "/ruta", None),
    ],
)
def test_policy_clean_value_bounds(key: str, value: object, expected: object) -> None:
    assert redaction.AttributePolicy().clean_value(key, value, redaction.OTHER) == expected


def test_register_extends_closed_lists_and_shares_routes() -> None:
    policy = redaction.AttributePolicy()
    policy.register("route", ["/v1/ledger/records/{record_id}"])
    assert policy.clean_value("http.route", "/v1/ledger/records/{record_id}", "x") != "x"
    with pytest.raises(KeyError):
        policy.register("session_id", ["a"])
    for bad in ["con espacio", "a@b", "https://x", "A" * 25, ""]:
        with pytest.raises(ValueError):
            policy.register("result", [bad])
    assert redaction.DEFAULT_POLICY.values("route") == frozenset()


def test_known_exception_type_only_names_loaded_classes() -> None:
    assert redaction.known_exception_type("ValueError")
    assert redaction.known_exception_type("builtins.KeyError")
    assert not redaction.known_exception_type("NoExisteEstaExcepcion")
    assert not redaction.known_exception_type(3)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
        "-----BEGIN CERTIFICATE-----\ncortado",
        "ver https://x.y/z?t=1 ahora",
        "ver WWW.x.y ahora",
        "de persona@correo.test hoy",
        "clave=Zm9vYmFyYmF6cXV4cXV1eDEyMzQ1Njc4",
    ],
)
def test_redact_text_patterns(text: str) -> None:
    assert redaction.REDACTED in redaction.redact_text(text)


@pytest.mark.parametrize(
    "text", ["registro escrito", "sesión abierta 3 veces", "/health/live", ORG.hex[:19]]
)
def test_redact_text_keeps_ordinary_messages(text: str) -> None:
    assert redaction.redact_text(text) == text
