"""PR-LAZ-25: toda atribución a persona y todo dato de contacto se rechazan, también disfrazados.

TASK-340 (BLM §14.5; BR-LAZ-84; A-45 punto 3 y su nota del 2026-09-30; LC-LAZ-16):

- **todo texto generado** con un cargo y un verbo de acción u omisión, un cargo como causa, una
  fórmula de error humano, un documento de identidad, un nombre propio, un correo, un teléfono o
  una placa se rechaza con ``PersonAttributionRejected`` (``person_attribution_detected``,
  ``detail_code = loop_person_attribution_detected``) nombrando el patrón que cayó;
- **también** con homoglifos cirílicos, espacios no ASCII (NBSP, U+3000, U+2009, U+202F), dígitos
  y signos de ancho completo e invisibles: con invisibles la política base de U-02 rechaza antes
  (``invisible_character``) y el validador solo, sin esa capa, sigue nombrando el mismo patrón;
- **los textos de control** con zonas, equipos y máquinas (``zone_equipment_texts``) se aceptan,
  también disfrazados con homoglifos, espacios no ASCII o ancho completo;
  los equipos con marca solo con su nombre declarado (sin él, el par cae como nombre propio);
- **bordes** de cada regla: 6 y 7 dígitos, 4 y 5 dígitos de documento, la palabra de vehículo a 3 y
  a 4 palabras de la placa, fechas y cifras con miles, una sola palabra con mayúscula, cargos sin
  verbo, clases de campo (``name``, ``identifier``, ``machine``, ``prose``) y la ruta exenta de
  §2.12;
- **vocabulario declarado** (``DeclaredNamesCache``): sin entrada se valida sin él; el preparador
  lo lee al abrir la escritura, como mucho una vez cada ``TTL_SECONDS``, y validar nunca lo lee.

Se comparan códigos y patrones, nunca subcadenas del texto (VIG-194). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from hypothesis import given

from tests.attribution_support import both_policy
from tests.properties.laz.strategies.texts import (
    AttributionText,
    AttributionVariant,
    ZoneText,
    attribution_unicode_texts,
    person_attribution_texts,
    zone_equipment_texts,
    zone_equipment_unicode_texts,
)
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextHint,
    FreeTextPreparation,
    FreeTextRejected,
    RowReader,
    canonical_form,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.validators.attribution import (
    COMMON_WORDS_VERSION,
    DETAIL_CODE,
    NO_DECLARED_NAMES,
    PERSON_ATTRIBUTION_DETECTED,
    AttributionPattern,
    FieldKind,
    PersonAttributionRejected,
    PersonAttributionValidator,
    declared_names,
    field_kind,
    find_attribution,
)
from vigia_platform.loop.validators.cache import TTL_SECONDS, DeclaredNamesCache
from vigia_platform.shared.api.errors import ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.clock import SimulatedClock

FIELD = FreeTextField("order_probe", "/note", 1, 2000)
POLICY = both_policy()


def _candidate(text: str) -> FreeTextCandidate:
    return FreeTextCandidate(text, canonical_form(text))


def _pattern(text: str, kind: FieldKind = FieldKind.PROSE) -> AttributionPattern | None:
    verdict = find_attribution(_candidate(text), kind=kind)
    return None if verdict is None else verdict.pattern


def _rejection(text: str) -> PersonAttributionRejected:
    with pytest.raises(PersonAttributionRejected) as raised:
        POLICY.apply(text, FIELD, organization_id=uuid.uuid4())
    return raised.value


# --- Propiedades ----------------------------------------------------------------------------------


@given(attribution=person_attribution_texts())
def test_every_attribution_is_rejected_naming_its_pattern(attribution: AttributionText) -> None:
    rejection = _rejection(attribution.text)
    assert rejection.verdict.pattern == attribution.pattern
    assert rejection.verdict.code == PERSON_ATTRIBUTION_DETECTED
    assert rejection.verdict.suggestion_es
    assert rejection.reason == PERSON_ATTRIBUTION_DETECTED
    assert rejection.detail_code == LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value
    # VIG-344: la pista para quien escribió es el motivo y la alternativa, nunca el término (P3).
    assert rejection.hint == FreeTextHint(
        code=PERSON_ATTRIBUTION_DETECTED, suggestion_es=rejection.verdict.suggestion_es
    )


@given(variant=attribution_unicode_texts(kinds=("homoglyph", "space", "fullwidth", "mixed")))
def test_a_disguised_attribution_falls_like_its_ascii_form(variant: AttributionVariant) -> None:
    assert _rejection(variant.text).verdict.pattern == variant.original.pattern


@given(variant=attribution_unicode_texts(kinds=("invisible",)))
def test_with_invisibles_the_base_policy_rejects_and_the_validator_alone_still_does(
    variant: AttributionVariant,
) -> None:
    with pytest.raises(FreeTextRejected) as raised:
        POLICY.apply(variant.text, FIELD, organization_id=uuid.uuid4())
    assert raised.value.reason == "invisible_character"
    assert _pattern(variant.text) == variant.original.pattern


@given(text=zone_equipment_texts())
def test_zone_equipment_and_machine_texts_are_accepted(text: ZoneText) -> None:
    policy = both_policy(text.declared)
    assert policy.apply(text.text, FIELD, organization_id=uuid.uuid4()) == text.text
    if text.declared:
        # El vocabulario declarado es lo que los exime: sin él, la marca cae como nombre propio.
        assert _rejection(text.text).verdict.pattern is AttributionPattern.PROPER_NAME


@given(text=zone_equipment_unicode_texts())
def test_a_disguised_control_text_is_accepted_like_its_ascii_form(text: ZoneText) -> None:
    # Los confusables se pliegan también en la forma con mayúsculas: «Prensa» con la a cirílica
    # (U+0430) es «Prensa», del vocabulario común o del declarado, y no un nombre propio.
    policy = both_policy(text.declared)
    assert policy.apply(text.text, FIELD, organization_id=uuid.uuid4())


# --- Bordes de cada regla ------------------------------------------------------------------------

EDGES: tuple[tuple[str, str, AttributionPattern | None], ...] = (
    # teléfono: siete dígitos o más (A-45 punto 3)
    ("seis dígitos", "Revisión del turno 123456", None),
    ("siete dígitos", "Revisión del turno 1234567", AttributionPattern.PHONE),
    ("siete con espacio", "Llamar al 300 1234", AttributionPattern.PHONE),
    ("fecha ISO", "Revisada el 2026-10-18 en la zona", None),
    ("fecha con barras", "Revisada el 18/10/2026 en la zona", None),
    ("miles con punto", "Costo de 1.500.000 pesos", None),
    ("miles con coma", "Costo de 1,500,000 pesos", None),
    ("norma con año", "Conforme a ISO 45001:2018", None),
    # documento: cinco dígitos o más tras la palabra
    ("documento de 4", "cédula 1234 en la planilla", None),
    ("documento de 5", "cédula 12345 en la planilla", AttributionPattern.IDENTITY_DOCUMENT),
    ("documento con n°", "documento n° 12345", AttributionPattern.IDENTITY_DOCUMENT),
    # placa: la palabra de vehículo a menos de cuatro palabras
    ("placa a 3", "matrícula de la unidad ABC123", AttributionPattern.VEHICLE_PLATE),
    ("placa a 4", "matrícula de la unidad móvil ABC123", None),
    ("código sin vehículo", "Procedimiento PRO-001 aplicado", None),
    ("placa de equipo", "La placa del equipo quedó suelta", None),
    ("moto", "la moto KLM 45D en el patio", AttributionPattern.VEHICLE_PLATE),
    # nombre propio: dos palabras con mayúscula fuera del vocabulario común o declarado
    ("una palabra", "Revisión con Pérez ausente", None),
    ("dos palabras", "Revisión con Juan Pérez", AttributionPattern.PROPER_NAME),
    ("mayúsculas", "REVISIÓN CON JUAN PÉREZ", AttributionPattern.PROPER_NAME),
    ("vocabulario común", "Revisión de la Prensa Norte", None),
    ("inicio de frase", "Revisión de la guarda", None),
    ("marca sin declarar", "Revisión de la Prensa Schuler Kompakt", AttributionPattern.PROPER_NAME),
    # cargos
    ("cargo sin verbo", "Capacitación del operario de la zona", None),
    ("cargo y verbo neutro", "El operario recibió capacitación", None),
    ("cargo y omisión", "El operario no usó el arnés", AttributionPattern.ROLE_ACTION),
    ("cargo con complemento", "el jefe de turno omitió", AttributionPattern.ROLE_ACTION),
    ("causa sin cargo", "Causa raíz: el proceso de bloqueo", None),
    ("causa con cargo", "Causa raíz: el operario", AttributionPattern.ROLE_AS_CAUSE),
    ("error de ingeniería", "Error de diseño en la guarda", None),
    ("error humano", "Error humano en la guarda", AttributionPattern.HUMAN_ERROR),
    # correo
    ("arroba suelta", "Turno @ 10 en la zona", None),
    ("correo", "Contacto ana.gomez@example.test", AttributionPattern.EMAIL),
    ("parte local de 64", f"Contacto {'a.' * 31}bc@example.test", AttributionPattern.EMAIL),
    ("puntos sin arroba", f"Registro {'a.' * 900}", None),
)


@pytest.mark.parametrize(
    ("text", "expected"), [(text, expected) for _, text, expected in EDGES],
    ids=[name for name, _, _ in EDGES],
)  # fmt: skip
def test_the_edges_of_each_rule(text: str, expected: AttributionPattern | None) -> None:
    assert _pattern(text) == expected
    if expected is None:
        assert POLICY.apply(text, FIELD, organization_id=uuid.uuid4()) == text
    else:
        assert _rejection(text).verdict.pattern is expected


def test_the_declared_vocabulary_exempts_only_its_pairs() -> None:
    declared = declared_names(["Línea Schuler Kompakt", "Planta Sabana Centro"])
    validator = PersonAttributionValidator(lambda _organization: declared)
    for text in ("Revisión de la Prensa Schuler Kompakt", "Visita a la SABANA CENTRO"):
        assert validator.verdict(_candidate(text)) is None, text
    for text in ("Revisión con Kompakt Schuler", "Revisión con Juan Pérez"):
        verdict = validator.verdict(_candidate(text))
        assert verdict is not None and verdict.pattern is AttributionPattern.PROPER_NAME, text


# --- Clases de campo y ruta exenta ---------------------------------------------------------------

KINDS: tuple[tuple[str, str, FieldKind], ...] = (
    ("classification", "/signer/display_name", FieldKind.NAME),
    ("plant_policy_signed", "/signed_by_display_name", FieldKind.NAME),
    ("user_account", "/display_name", FieldKind.NAME),
    ("zone", "/name", FieldKind.NAME),
    ("zone_created", "/name", FieldKind.NAME),
    ("closure", "/signer/professional_license", FieldKind.IDENTIFIER),
    ("finding_received", "/cameras[*]/clips[*]/storage_key", FieldKind.MACHINE),
    ("finding_received", "/node_time/clock/source", FieldKind.MACHINE),
    ("action_evidence_added", "/attachments[*]/filename", FieldKind.IDENTIFIER),
    ("plant_created", "/timezone", FieldKind.MACHINE),
    ("classification", "/reason_text", FieldKind.PROSE),
    ("zone", "/description", FieldKind.PROSE),
    ("organization_created", "/display", FieldKind.PROSE),
)


@pytest.mark.parametrize(("record_type", "path", "kind"), KINDS)
def test_field_kind(record_type: str, path: str, kind: FieldKind) -> None:
    assert field_kind(FreeTextField(record_type, path, 1, 200)) is kind


def test_a_name_field_takes_a_name_but_not_contact_data() -> None:
    signer = FreeTextField("closure", "/signer/display_name", 1, 120)
    assert POLICY.apply("Ana Gómez", signer) == "Ana Gómez"
    for text in ("ana.gomez@example.test", "Ana 300 123 4567", "Ana cédula 1020304050"):
        with pytest.raises(PersonAttributionRejected):
            POLICY.apply(text, signer)


def test_an_identifier_field_takes_digits_and_codes_but_not_an_email() -> None:
    license_ = FreeTextField("closure", "/signer/professional_license", 1, 64)
    key = FreeTextField("finding_received", "/cameras[*]/clips[*]/storage_key", 1, 512)
    assert POLICY.apply("TP 1234567-2020 ABC-123", license_) == "TP 1234567-2020 ABC-123"
    stored = "org/1234-5678/zone/9012-3456/clip.mp4"
    assert POLICY.apply(stored, key) == stored
    with pytest.raises(PersonAttributionRejected):
        POLICY.apply("ana.gomez@example.test", license_)


def test_the_declared_exempt_path_of_the_vocabulary_list_is_not_validated() -> None:
    validator = PersonAttributionValidator()
    exempt = FreeTextField("vocabulary_list_changed", "/added[*]/value", 1, 200)
    validator(_candidate("falla humana"), exempt)  # el término que se quiere bloquear
    with pytest.raises(PersonAttributionRejected):
        reason = FreeTextField("vocabulary_list_changed", "/reason", 1, 500)
        validator(_candidate("falla humana"), reason)


# --- Códigos -------------------------------------------------------------------------------------


def test_the_detail_code_reaches_the_api() -> None:
    assert LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value == DETAIL_CODE
    rejection = LedgerRejection.of(
        LedgerRejectionCode.FREE_TEXT_REJECTED, "/note", detail_code=DETAIL_CODE
    )
    error = from_ledger_rejection(rejection)
    assert (error.code, error.detail_code) == (ApiErrorCode.INVALID_REQUEST, DETAIL_CODE)


def test_the_common_words_file_is_versioned() -> None:
    assert COMMON_WORDS_VERSION.startswith("common_words.es/v1+sha256:")


# --- Vocabulario declarado: caché fuera de la ruta de validación ---------------------------------


@dataclass
class MemoryNames:
    names_by_organization: dict[uuid.UUID, tuple[str, ...]] = field(default_factory=dict)
    reads: int = 0

    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        self.reads += 1
        return self.names_by_organization.get(organization_id, ())


async def _no_rows(*_: object) -> list[object]:
    raise AssertionError("la caché en memoria no lee filas")


def test_the_declared_names_cache_reads_on_preparation_only_and_expires() -> None:
    organization = uuid.uuid4()
    store = MemoryNames({organization: ("Prensa Schuler Kompakt",)})
    cache = DeclaredNamesCache(store)
    clock = SimulatedClock(datetime(2026, 10, 18, tzinfo=UTC))
    validator = PersonAttributionValidator(cache.names_for)
    text = _candidate("Revisión de la Prensa Schuler Kompakt")
    text = FreeTextCandidate(text.text, text.canonical, organization)
    assert cache.names_for(organization) is NO_DECLARED_NAMES
    assert validator.verdict(text) is not None  # sin cargar: lo más estricto
    assert store.reads == 0

    preparation = FreeTextPreparation(organization, _no_rows, clock)
    asyncio.run(cache.refresh(preparation))
    assert store.reads == 1
    for _ in range(3):
        assert validator.verdict(text) is None
    assert cache.names_for(None) is NO_DECLARED_NAMES
    assert store.reads == 1  # validar nunca lee

    clock.advance(TTL_SECONDS - 1)
    asyncio.run(cache.refresh(preparation))
    assert store.reads == 1  # vigente
    clock.advance(1)
    asyncio.run(cache.refresh(preparation))
    assert store.reads == 2  # caducada: se relee

    cache.invalidate(organization)
    assert cache.names_for(organization) is NO_DECLARED_NAMES
    with pytest.raises(ValueError, match="positivo"):
        DeclaredNamesCache(store, ttl_seconds=0)
