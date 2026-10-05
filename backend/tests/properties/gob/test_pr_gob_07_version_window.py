"""PR-GOB-07 en las rutas de la ingesta: la decisión de versión es la de ``is_compatible`` de U-01.

BR-GOB-87, BR-CTR-18 a 25 y la nota T-05 (TASK-221). Para toda terna del kit (``version_pair``,
el ``version_triples`` del diseño: declarada, vigente, ventana, menores en aviso de retiro,
publicación de la mayor y última menor de la anterior, y el instante de la evaluación), la
``NodeApiGate`` real con esa política y el reloj en ese instante responde a un hallazgo, una
detección o un evento por lo demás válidos:

- ``200`` si y solo si ``is_compatible`` acepta (``accepted`` o ``accepted_with_notice``);
- si rechaza, ``code = rejection_code(resultado)`` (``contract_version_unsupported`` o
  ``contract_version_retired``) con ``compatibility_result`` = el resultado; ``rejected_newer``
  **nunca** es un ``code`` (va como ``compatibility_result`` de ``contract_version_unsupported``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    VersionPair,
    detection_for_review,
    finding,
    observability_event,
    version_pair,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response
from vigia_contracts.versioning import Version, is_accepted, is_compatible, rejection_code

from tests.ingest_support import ingest_world, place
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.node_api.versioning import RetiringMinor, VersionPolicy

YEAR = dt.timedelta(days=365)
HOUR = dt.timedelta(hours=1)


def _policy(pair: VersionPair) -> VersionPolicy:
    major = Version.parse(pair.current).major
    return VersionPolicy(
        current=Version.parse(pair.current),
        window=pair.window,
        retiring=tuple(
            RetiringMinor(f"{item.major}.{item.minor}.0", item.retires_at) for item in pair.retiring
        ),
        latest_minors=(
            {major - 1: pair.previous_major_last_minor}
            if pair.previous_major_last_minor is not None
            else {}
        ),
        major_published_at={major: pair.current_major_published_at},
    )


def _strategy(kind: IngestKind, catalog: dict[str, Any], node_id: str) -> st.SearchStrategy[Any]:
    if kind is IngestKind.FINDING:
        return finding(catalog, node_id=node_id)
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return detection_for_review(catalog, node_id=node_id)
    return observability_event(catalog, node_id=node_id)


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(
    data=st.data(),
    pair=version_pair(),
    kind=st.sampled_from(tuple(IngestKind)),
    catalog=zone_catalog(),
)
def test_pr_gob_07_the_version_decision_is_the_one_of_is_compatible(
    data: st.DataObject, pair: VersionPair, kind: IngestKind, catalog: dict[str, Any]
) -> None:
    evaluated_at = dt.datetime.fromisoformat(pair.evaluated_at)
    policy = _policy(pair)
    world = ingest_world(start=evaluated_at, policy=policy)
    scoped = world.scoped(catalog)
    world.publish_catalog(scoped, world.now - YEAR)
    world.set_usage(True, world.now - YEAR)
    document = place(
        data.draw(_strategy(kind, scoped, str(world.a.node_id)), label="document"),
        world.now - HOUR,
    )
    document["contract_version"] = pair.declared
    response = world.post(
        kind, document, headers=world.headers(document, kind, version=pair.declared)
    )

    expected = is_compatible(
        pair.declared,
        policy.current,
        today=evaluated_at,
        window=policy.window,
        retiring=policy.retiring,
        latest_minors=policy.latest_minors,
        major_published_at=policy.major_published_at,
    )
    if is_accepted(expected):
        assert response.status_code == 200, (pair, response.text)
        return
    assert response.status_code == 400, (pair, response.text)
    rejection = parse_rejection_response(response.content)
    assert rejection.code.value != "rejected_newer"
    assert rejection.code == rejection_code(expected)
    assert rejection.compatibility_result is not None
    assert rejection.compatibility_result.value == expected.value
    assert world.accepted(kind) == []
