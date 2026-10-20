"""``NotificationPreferenceUpdateIn`` (TASK-348; SUP-U05-35; BR-LAZ-126): sin campo para silenciar
``immediate``. ``test_pr_nuc_38_every_mutated_body_is_a_generic_rejection`` ya prueba, para
``PUT /notifications/preferences`` como para toda ruta con cuerpo, que un campo extra cualquiera
cae en ``invalid_request``; esta prueba nombra el campo concreto que BR-LAZ-126 declara imposible
de estructura, no solo «algún campo extra».
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vigia_platform.notifications.adapters.http.routes import NotificationPreferenceUpdateIn

_VALID_BODY = {
    "email_enabled_normal": True,
    "email_enabled_digest": True,
    "weekly_digest_weekday": 1,
}


def test_the_update_body_has_exactly_the_three_documented_fields() -> None:
    assert set(NotificationPreferenceUpdateIn.model_fields) == {
        "email_enabled_normal",
        "email_enabled_digest",
        "weekly_digest_weekday",
    }


def test_an_immediate_silencing_field_is_rejected_by_the_schema_itself() -> None:
    """BR-LAZ-126: no hay campo para silenciar ``immediate``, ni siquiera nombrándolo (``extra =
    "forbid"``, no una validación de negocio que pudiera quedar fuera)."""
    with pytest.raises(ValidationError):
        NotificationPreferenceUpdateIn.model_validate(
            {**_VALID_BODY, "email_enabled_immediate": False}
        )
