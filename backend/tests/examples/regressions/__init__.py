"""Regresiones permanentes: contraejemplos reducidos de las propiedades (NFR-NUC-48, PBT-10).

Cuando una propiedad de ``tests/properties/`` falla, Hypothesis reduce el contraejemplo. Después de
corregir el defecto, ese caso reducido entra aquí como prueba de ejemplo **fija**, para que no
dependa de que una semilla vuelva a encontrarlo:

- un módulo ``test_pr_nuc_NN_<qué>.py`` por propiedad (o por defecto, si una propiedad dio varios);
- su docstring nombra la propiedad (``PR-NUC-NN``), la semilla o el ``@reproduce_failure`` con que
  se encontró y el defecto que delataba;
- el caso va tal cual lo redujo Hypothesis (sin «limpiarlo»), con datos solo generados.

``tests/examples/test_regressions_catalog.py`` comprueba esa forma en cada módulo de esta carpeta.

Los contraejemplos anteriores a esta carpeta se quedan junto a su propiedad (p. ej. las claves
públicas de orden pequeño de ``tests/properties/test_signing_port.py``).
"""
