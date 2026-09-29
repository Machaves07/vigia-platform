"""Pila base de U-02: nombre con sufijo de entorno, región fija y calificador ``vigia``."""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import DefaultStackSynthesizer, Environment, Stack
from constructs import Construct

from config import BOOTSTRAP_QUALIFIER, EnvironmentConfig


class VigiaStack(Stack):
    """Pila de U-02 ligada a la configuración de su despliegue.

    La cuenta no se fija: los nombres que la llevan los resuelve CloudFormation al
    desplegar (``AWS::AccountId``), así que ``cdk synth`` no necesita credenciales ni hace
    búsquedas contra AWS. La región sí se fija (NFR-NUC-31).
    """

    #: Nombre corto de la pila (``foundation``, ``data``...); lo fija cada subclase.
    key: str = ""
    #: Descripción de la plantilla, en español sin tildes (CloudFormation la muestra tal cual).
    summary: str = ""

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        """``tags`` son las etiquetas globales: con ``@aws-cdk/core:explicitStackTags`` la pila
        solo lleva las que recibe aquí, y CloudFormation las propaga a sus recursos."""
        if not self.key:
            raise TypeError(f"{type(self).__name__} no declara el nombre corto de la pila")
        name = config.stack_name(self.key)
        super().__init__(
            scope,
            name,
            stack_name=name,
            env=Environment(region=config.region),
            synthesizer=DefaultStackSynthesizer(qualifier=BOOTSTRAP_QUALIFIER),
            description=f"Vigia U-02 ({config.deployment}): {self.summary}",
            tags=dict(tags),
        )
        self.config = config
