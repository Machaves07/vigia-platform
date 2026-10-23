"""Genera ``backend/openapi/u05-fixtures.json``: los oráculos de U-05 que solo el backend conoce.

Uso (desde ``backend/``)::

    uv run python -m vigia_platform.shared.api.export_u05_fixtures          # escribe el archivo
    uv run python -m vigia_platform.shared.api.export_u05_fixtures --check  # 0 si no hay diferencia

El archivo tiene dos secciones, que ``frontend/`` consume por igualdad y no por heurística:

- ``permissions`` (BR-APP-02, PR-APP-01, PAT-APP-SEG-06): el registro consolidado de claves de
  ``PermissionKey`` (U-02 §2.6 con las notas fechadas de U-03 y U-04, A-13), la columna de cada uno
  de los siete roles tal como la guarda ``MATRIX`` y la columna **efectiva** de cada rol en los
  tres contextos en que la evalúa ``identity.authz.authorize``: en una organización cliente, en
  la organización proveedora (las claves ``platform.*`` y los roles del proveedor solo valen
  allí) y en un cliente bajo concesión (solo el ``provider_installer``, con exactamente su
  columna; BR-NUC-37). Este módulo solo **lee** ``identity.authz`` (ruta del dueño):
  ``tests/unit/test_u05_fixtures_export.py`` contrasta cada columna efectiva con ``route_role``.
- ``free_text`` (BR-APP-42 y 44, PR-APP-12): los límites de ``FreeTextPolicy`` de cada ruta de
  ``free_text_paths`` de los tipos de registro de todas las unidades (última versión de cada
  tipo, como escribe el escritor del expediente) y un corpus determinista de vectores
  ``{case, route, text, verdict, normalized}`` cuyo veredicto da la propia política, con semilla
  fija. La política es la **base** de U-02 (``FreeTextPolicyRegistry`` sin validadores): NFC,
  caracteres de control, invisibles (A-45), no admitidos, marcado y longitud. Los validadores
  enchufados de U-04 (vocabulario y atribución) no entran, porque la interfaz solo replica las
  restricciones estructurales y el mensaje del servidor prevalece (BR-APP-42).

``--check`` compara byte a byte con el archivo versionado y termina en 1 si difiere o falta;
entonces imprime cada diferencia con su clave, rol, ruta o caso, y el principio del diff. El JSON
se escribe con ``ensure_ascii``: todo carácter invisible o de control queda escrito como
``\\uXXXX`` y se ve en la revisión.
"""

from __future__ import annotations

import argparse
import difflib
import enum
import json
import random
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from vigia_platform.identity.authz.matrix import MATRIX, PLATFORM_PREFIX, PermissionKey
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextRejected,
    FreeTextRejection,
)
from vigia_platform.shared.context import Role
from vigia_platform.shared.runtime.units import record_type_registry, registered_units

__all__ = [
    "ACCEPTED",
    "CORPUS_SEED",
    "DEFAULT_OUTPUT",
    "FORMAT",
    "PROVIDER_ONLY_ROLES",
    "SCHEMA_VERSION",
    "EvaluationContext",
    "FreeTextRoute",
    "base_policy",
    "build_document",
    "differences",
    "evaluate",
    "free_text_routes",
    "free_text_section",
    "main",
    "permission_differences",
    "permissions_section",
    "render",
]

DEFAULT_OUTPUT: Final = Path(__file__).resolve().parents[4] / "openapi/u05-fixtures.json"
"""``backend/openapi/u05-fixtures.json``."""
FORMAT: Final = "vigia-platform/u05-fixtures"
SCHEMA_VERSION: Final = 1
"""Versión del formato del archivo: sube si cambia la forma, no si cambian los datos."""
CORPUS_SEED: Final = 462
"""Semilla del corpus (TASK-462): elige rutas y relleno; el mismo código da el mismo archivo."""
ACCEPTED: Final = "accepted"
"""Veredicto de un texto que pasa la política; el resto son los motivos de ``FreeTextRejection``."""
_COMMAND: Final = "uv run python -m vigia_platform.shared.api.export_u05_fixtures"
_DIFF_LINES: Final = 60

PROVIDER_ONLY_ROLES: Final = (Role.PROVIDER_INSTALLER, Role.PLATFORM_OPERATOR)
"""Los roles que solo conceden en la organización proveedora (``identity.authz.authorize``)."""
_CONCESSION_ROLE: Final = Role.PROVIDER_INSTALLER
"""Bajo concesión cuenta solo esta asignación, con exactamente su columna (BR-NUC-37)."""


class EvaluationContext(enum.StrEnum):
    """Los contextos en que ``authorize`` evalúa la matriz de forma distinta."""

    CUSTOMER_ORGANIZATION = "customer_organization"
    """Una organización cliente, sin concesión: sin ``platform.*`` ni roles del proveedor."""
    PROVIDER_ORGANIZATION = "provider_organization"
    """La organización proveedora, sin concesión: solo los roles del proveedor."""
    CUSTOMER_UNDER_CONCESSION = "customer_under_concession"
    """Un cliente bajo concesión: solo ``provider_installer``, sin ``platform.*``."""


# --- Matriz de permisos -----------------------------------------------------------------------


def _effective(context: EvaluationContext, role: Role, keys: Collection[str]) -> tuple[str, ...]:
    platform = {key for key in keys if key.startswith(PLATFORM_PREFIX)}
    if context is EvaluationContext.PROVIDER_ORGANIZATION:
        granted = set(keys) if role in PROVIDER_ONLY_ROLES else set()
    elif context is EvaluationContext.CUSTOMER_UNDER_CONCESSION:
        granted = set(keys) - platform if role is _CONCESSION_ROLE else set()
    else:
        granted = set() if role in PROVIDER_ONLY_ROLES else set(keys) - platform
    return tuple(sorted(granted))


def permissions_section(
    keys: Iterable[str], matrix: Mapping[Role, Collection[str]]
) -> dict[str, Any]:
    """La sección ``permissions`` para ``keys`` y ``matrix`` (en el archivo, las de ``MATRIX``)."""
    registered = sorted({str(key) for key in keys})
    columns = {role: tuple(sorted(str(key) for key in matrix[role])) for role in Role}
    return {
        "source": "identity/authz/matrix.py (MATRIX) evaluada como identity/authz/authorize.py",
        "platform_prefix": PLATFORM_PREFIX,
        "keys": registered,
        "platform_keys": [key for key in registered if key.startswith(PLATFORM_PREFIX)],
        "roles": [role.value for role in Role],
        "provider_only_roles": [role.value for role in PROVIDER_ONLY_ROLES],
        "matrix": {role.value: list(columns[role]) for role in Role},
        "effective": {
            context.value: {
                role.value: list(_effective(context, role, columns[role])) for role in Role
            }
            for context in EvaluationContext
        },
    }


def _names(value: object) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def _column_differences(
    committed: object, current: object, what: tuple[str, ...]
) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    old = committed if isinstance(committed, dict) else {}
    new = current if isinstance(current, dict) else {}
    for role in sorted(set(old) | set(new)):
        before, after = _names(old.get(role)), _names(new.get(role))
        found.extend(("missing_in_file", *what, role, key) for key in sorted(after - before))
        found.extend(("extra_in_file", *what, role, key) for key in sorted(before - after))
    return found


def permission_differences(
    committed: Mapping[str, Any], current: Mapping[str, Any]
) -> list[tuple[str, ...]]:
    """Las diferencias entre la sección versionada y la del código, nombrando clave y rol.

    ``("missing_in_file", "key", k)``: ``k`` está en el código y no en el archivo;
    ``("extra_in_file", "rule", rol, k)``: el archivo da ``k`` a ``rol`` y el código no;
    ``("effective", contexto, …)`` igual sobre la columna efectiva. Vacía si coinciden.
    """
    found: list[tuple[str, ...]] = []
    for name in ("keys", "roles"):
        before, after = _names(committed.get(name)), _names(current.get(name))
        found.extend(("missing_in_file", name[:-1], item) for item in sorted(after - before))
        found.extend(("extra_in_file", name[:-1], item) for item in sorted(before - after))
    found.extend(_column_differences(committed.get("matrix"), current.get("matrix"), ("rule",)))
    old = committed.get("effective")
    old_contexts = old if isinstance(old, dict) else {}
    for context, columns in current["effective"].items():
        found.extend(
            _column_differences(old_contexts.get(context), columns, ("effective", context))
        )
    found.extend(
        ("field_differs", name)
        for name in sorted(set(committed) | set(current))
        if name not in {"keys", "roles", "matrix", "effective"}
        and committed.get(name) != current.get(name)
    )
    return found


# --- Texto libre ------------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class FreeTextRoute:
    """Una ruta de texto libre declarada: ``route`` es el tipo de registro seguido de su ruta."""

    route: str
    record_type: str
    path: str
    min_length: int
    max_length: int

    @property
    def field(self) -> FreeTextField:
        return FreeTextField(self.record_type, self.path, self.min_length, self.max_length)


def free_text_routes() -> tuple[FreeTextRoute, ...]:
    """Cada ruta de ``free_text_paths`` de la última versión de cada tipo, con sus límites."""
    registry = record_type_registry(registered_units())
    return tuple(
        sorted(
            FreeTextRoute(
                route=field.record_type + field.path,
                record_type=field.record_type,
                path=field.path,
                min_length=field.min_length,
                max_length=field.max_length,
            )
            for compiled in registry.latest()
            for field in compiled.free_text_fields.values()
        )
    )


def base_policy() -> FreeTextPolicyRegistry:
    """``FreeTextPolicy`` sin validadores enchufados: la política base de U-02, sellada."""
    registry = FreeTextPolicyRegistry()
    registry.seal()
    return registry


def evaluate(
    policy: FreeTextPolicyRegistry, route: FreeTextRoute, text: str
) -> tuple[str, str | None]:
    """El veredicto de ``policy`` sobre ``text`` en ``route`` y el texto NFC si lo acepta."""
    try:
        normalized = policy.apply(text, route.field)
    except FreeTextRejected as rejection:
        return rejection.reason, None
    return ACCEPTED, normalized


_FILLER: Final = (
    "zona",
    "baliza",
    "turno",
    "revisión",
    "señal",
    "cámara",
    "acción",
    "cierre",
    "planta",
    "estándar",
    "línea",
    "acreditación",
)
"""Palabras sintéticas del relleno (con tildes precompuestas, que NFC deja igual)."""

_CHARACTERS: Final = (
    # Control (Cc, Zl, Zp).
    0x0000,
    0x0009,
    0x000A,
    0x000D,
    0x001F,
    0x007F,
    0x0085,
    0x2028,
    0x2029,
    # Formato invisible (Cf).
    0x00AD,
    0x061C,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x202E,
    0x2060,
    0xFEFF,
    0x1D173,
    0xE0001,
    # Ignorables por defecto que no son Cf, y el braille en blanco (A-45).
    0x034F,
    0x115F,
    0x1160,
    0x17B4,
    0x180B,
    0x2800,
    0x3164,
    0xFE00,
    0xFE0F,
    0xFFA0,
    0xE0100,
    # Sustitutos sueltos (Cs), uso privado (Co) y no caracteres (Cn).
    0xD800,
    0xDFFF,
    0xE000,
    0xF8FF,
    0xF0000,
    0xFFFE,
    0xFFFF,
    0x10FFFF,
    # Espacios Unicode (Zs): se admiten; solo la forma canónica de los validadores los pliega.
    0x00A0,
    0x2003,
    0x202F,
    0x3000,
    # Fuera del plano básico: un carácter, dos unidades UTF-16.
    0x1F600,
    0x1D400,
)

_SNIPPETS: Final = (
    ("markup:tag", "<b>"),
    ("markup:tag-upper", "<B>"),
    ("markup:closing-tag", "</p>"),
    ("markup:comment", "<!--"),
    ("markup:processing-instruction", "<?xml"),
    ("markup:script", "<script>"),
    ("markup:named-entity", "&lt;"),
    ("markup:named-entity-upper", "&LT;"),
    ("markup:named-entity-any", "&copy;"),
    ("markup:legacy-entity-lt", "&ltscript"),
    ("markup:legacy-entity-gt", "&gt"),
    ("markup:legacy-entity-amp", "&amp"),
    ("markup:legacy-entity-prefix", "&ampersand"),
    ("markup:legacy-entity-quot", "&quot"),
    ("markup:legacy-entity-apos", "&apos"),
    ("markup:legacy-entity-nbsp", "&nbsp"),
    ("markup:legacy-entity-lte", "&lte"),
    ("markup:decimal-entity", "&#60"),
    ("markup:decimal-entity-closed", "&#60;"),
    ("markup:hex-entity", "&#x3C"),
    ("markup:hex-entity-upper", "&#X3c;"),
    ("markup:fullwidth-tag", "\uff1cb\uff1e"),
    ("markup:small-form-tag", "\ufe64b\ufe65"),
    ("markup:fullwidth-digit-entity", "&#\uff16\uff10"),
    ("markup:word-entity", "a&b;"),
    ("plain:less-than-space", "a < b"),
    ("plain:greater-than", "3 > 2"),
    ("plain:less-than-digit", "<1"),
    ("plain:ampersand-word", "R&D"),
    ("plain:ampersand-letters", "AT&T"),
    ("plain:ampersand-alone", "&"),
    ("plain:ampersand-hash", "&#"),
    ("plain:ampersand-hash-x", "&#x"),
    ("plain:ampersand-semicolon", "&;"),
    ("plain:ampersand-unterminated", "a&b"),
    ("nfc:combining-acute", "Cafe\u0301"),
    ("nfc:combining-ring", "A\u030a"),
    ("nfc:angstrom-sign", "\u212b"),
    ("nfc:hangul-jamo", "\u1100\u1161\u11a8"),
    ("nfc:already-composed", "Caf\u00e9"),
    ("nfc:ligature-kept", "\ufb01"),
    ("nfc:fullwidth-kept", "\uff21\uff11"),
)
"""Fragmentos con su caso: marcado y sus vecinos admitidos, y normalización NFC."""


class _Corpus:
    def __init__(self, routes: Sequence[FreeTextRoute], policy: FreeTextPolicyRegistry) -> None:
        self._routes = routes
        self._policy = policy
        self._random = random.Random(CORPUS_SEED)  # noqa: S311 — semilla fija, no es secreto
        self.vectors: list[dict[str, Any]] = []

    def filler(self, length: int) -> str:
        text = ""
        while len(text) < length:
            text += self._filler_word() + " "
        return text[:length]

    def _filler_word(self) -> str:
        return _FILLER[self._random.randrange(len(_FILLER))]

    def route_for(self, length: int) -> FreeTextRoute:
        """Una ruta de la semilla donde caben ``length`` caracteres."""
        fitting = [route for route in self._routes if route.max_length >= length]
        return fitting[self._random.randrange(len(fitting))]

    def add(self, case: str, route: FreeTextRoute, text: str) -> None:
        verdict, normalized = evaluate(self._policy, route, text)
        self.vectors.append(
            {
                "case": case,
                "route": route.route,
                "text": text,
                "verdict": verdict,
                "normalized": normalized,
            }
        )

    def add_fitted(self, case: str, snippet: str) -> None:
        """``snippet`` en una ruta de la semilla, con relleno hasta su mínimo y algo más.

        Va entre espacios: una letra del relleno pegada al fragmento cambiaría su caso (``&#x``
        seguido de ``c`` ya es una entidad hexadecimal).
        """
        core = f" {snippet} "
        route = self.route_for(len(core) + 1)
        room = route.max_length - len(core)
        padding = max(route.min_length - len(core), 0)
        padding += self._random.randrange(max(min(room - padding, 16), 0) + 1)
        before = self._random.randrange(padding + 1)
        self.add(case, route, self.filler(before) + core + self.filler(padding - before))


def _corpus(routes: Sequence[FreeTextRoute], policy: FreeTextPolicyRegistry) -> list[Any]:
    corpus = _Corpus(routes, policy)
    for route in routes:
        for case, length in (
            ("length:below-min", route.min_length - 1),
            ("length:min", route.min_length),
            ("length:max", route.max_length),
            ("length:above-max", route.max_length + 1),
        ):
            if length >= 0:
                corpus.add(f"{case}:{route.route}", route, corpus.filler(length))
    for code in _CHARACTERS:
        corpus.add_fitted(f"character:U+{code:04X}", chr(code))
    for case, snippet in _SNIPPETS:
        corpus.add_fitted(case, snippet)
    # Longitud en caracteres tras NFC (no en unidades UTF-16 ni antes de normalizar), y el orden
    # de las reglas: tope previo de cuatro veces el máximo, caracteres, marcado y longitud.
    short = min(routes, key=lambda route: (route.max_length, route.route))
    reasoned = max(routes, key=lambda route: (route.min_length, route.route))
    top = short.max_length
    for case, route, text in (
        ("nfc-length:combining-at-max", short, "e\u0301" * top),
        ("nfc-length:combining-above-max", short, "e\u0301" * (top + 1)),
        ("nfc-length:jamo-at-max", short, "\u1100\u1161" * top),
        ("utf16-length:astral-at-max", short, "\U0001f600" * top),
        ("utf16-length:astral-above-max", short, "\U0001f600" * (top + 1)),
        ("order:expansion-before-control", short, "\x00" + "a" * (4 * top)),
        ("order:expansion-limit-control", short, "\x00" + "a" * (4 * top - 1)),
        ("order:control-before-markup", short, "\x00<b>"),
        ("order:invisible-before-markup", short, "\u200b<b>"),
        ("order:markup-before-too-long", short, "<b>" + "a" * top),
        ("order:markup-before-too-short", reasoned, "<b>"),
        ("order:empty", reasoned, ""),
    ):
        corpus.add(case, route, text)
    return corpus.vectors


def free_text_section(
    routes: Sequence[FreeTextRoute], policy: FreeTextPolicyRegistry
) -> dict[str, Any]:
    """La sección ``free_text`` para ``routes`` evaluada con ``policy``."""
    return {
        "source": "ledger/free_text.py (FreeTextPolicyRegistry sin validadores enchufados)",
        "policy": "base",
        "verdicts": [ACCEPTED, *(reason.value for reason in FreeTextRejection)],
        "length_unit": "code_point_after_nfc",
        "seed": CORPUS_SEED,
        "routes": [
            {
                "route": route.route,
                "record_type": route.record_type,
                "path": route.path,
                "min_length": route.min_length,
                "max_length": route.max_length,
            }
            for route in routes
        ],
        "vectors": _corpus(routes, policy),
    }


# --- Documento --------------------------------------------------------------------------------


def build_document(
    keys: Iterable[str],
    matrix: Mapping[Role, Collection[str]],
    routes: Sequence[FreeTextRoute],
    policy: FreeTextPolicyRegistry,
) -> dict[str, Any]:
    """El documento completo, con su cabecera de formato."""
    return {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "generated": {
            "by": _COMMAND,
            "check": f"{_COMMAND} --check",
            "notice": (
                "Generado: no se edita a mano. La canalización exige --check sin diferencia. "
                "Se consume por igualdad: permissions.effective[contexto][rol] son las claves que "
                "concede la asignación de ese rol en ese contexto; free_text.vectors[].verdict es "
                "'accepted' o el motivo de rechazo de la política base, y normalized, el texto "
                "NFC que se guarda (null si se rechaza). Las longitudes cuentan caracteres "
                "Unicode tras NFC, no unidades UTF-16."
            ),
        },
        "permissions": permissions_section(keys, matrix),
        "free_text": free_text_section(routes, policy),
    }


def render() -> str:
    """El contenido de ``u05-fixtures.json`` para el código actual."""
    document = build_document(PermissionKey, MATRIX, free_text_routes(), base_policy())
    return json.dumps(document, ensure_ascii=True, indent=2) + "\n"


def differences(committed: object, current: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Diferencias con nombre entre el documento versionado y el del código."""
    if not isinstance(committed, dict):
        return [("invalid_file",)]
    found: list[tuple[str, ...]] = [
        ("field_differs", name)
        for name in ("format", "schema_version", "generated")
        if committed.get(name) != current[name]
    ]
    permissions = committed.get("permissions")
    found.extend(
        permission_differences(
            permissions if isinstance(permissions, dict) else {}, current["permissions"]
        )
    )
    old = committed.get("free_text")
    old_text: dict[str, Any] = old if isinstance(old, dict) else {}
    new_text = current["free_text"]
    for name in sorted(set(old_text) | set(new_text)):
        if name in {"routes", "vectors"}:
            continue
        if old_text.get(name) != new_text.get(name):
            found.append(("field_differs", f"free_text.{name}"))
    for name, key in (("routes", "route"), ("vectors", "case")):
        before = _indexed(old_text.get(name), key)
        after = _indexed(new_text[name], key)
        found.extend(
            ("missing_in_file", name[:-1], item) for item in sorted(set(after) - set(before))
        )
        found.extend(
            ("extra_in_file", name[:-1], item) for item in sorted(set(before) - set(after))
        )
        found.extend(
            ("differs_in_file", name[:-1], item)
            for item in sorted(set(before) & set(after))
            if before[item] != after[item]
        )
    return found


def _indexed(items: object, key: str) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    return {str(item[key]): item for item in items if isinstance(item, dict) and key in item}


def _report(output: Path, current_text: str, committed_text: str) -> None:
    try:
        committed: object = json.loads(committed_text)
    except ValueError:
        committed = None
    for difference in differences(committed, json.loads(current_text)):
        print("diferencia: " + " ".join(difference), file=sys.stderr)
    diff = difflib.unified_diff(
        committed_text.splitlines(),
        current_text.splitlines(),
        fromfile=f"{output} (versionado)",
        tofile=f"{output} (generado)",
        lineterm="",
    )
    for number, line in enumerate(diff):
        if number == _DIFF_LINES:
            print("…", file=sys.stderr)
            break
        print(line, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_u05_fixtures",
        description=(
            "Genera o comprueba backend/openapi/u05-fixtures.json (matriz de permisos y vectores "
            "de la política de texto libre para U-05)."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="no escribe: termina en 1 si el archivo versionado difiere de lo generado",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="ruta del archivo (por defecto, backend/openapi/u05-fixtures.json)",
    )
    arguments = parser.parse_args(argv)
    text = render()
    content = text.encode("utf-8")
    output: Path = arguments.output
    if arguments.check:
        try:
            current = output.read_bytes()
        except OSError:
            print(f"falta {output}: genéralo sin --check", file=sys.stderr)
            return 1
        if current != content:
            print(
                f"{output} no coincide con la matriz y la política actuales: regenéralo con "
                f"`{_COMMAND}` y revisa el cambio",
                file=sys.stderr,
            )
            _report(output, text, current.decode("utf-8", errors="replace"))
            return 1
        print(f"{output} coincide con la matriz y la política actuales")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    print(f"escrito {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
