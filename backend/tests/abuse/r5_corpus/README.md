# Corpus de casos trampa de R5 (U-04)

Carpeta de los **casos trampa** del escenario de ataque R5 del plan de evaluación del PRD
(atribución a persona y reidentificación; escenario L-5): LC-LAZ-43, NFR-LAZ-48, `tests/abuse/` y
el validador de atribución de TASK-340. La creó TASK-302 vacía para que las tareas de U-04 que la
llenan no choquen en archivos compartidos.

Formato (TASK-340): un archivo YAML por tema, cada uno con su `version` y su `description`, y una
lista `cases`. Cada caso lleva:

- `id` (`R5-<TEMA>-nn`), `text` y `expected` (`reject` o `accept`);
- `code`: el motivo del rechazo (`FreeTextRejected.reason`): `person_attribution_detected`,
  `blocked_vocabulary_term` o, si lo detiene antes la política base de U-02,
  `invisible_character` o `markup`;
- `pattern`: el patrón de `person_attribution` que debe caer, si aplica;
- `declared`: el vocabulario declarado de la organización del caso (nombres de plantas y zonas);
- `reason`: el **motivo** del caso.

| Archivo | Qué cubre |
|---|---|
| `attribution.yaml` | Cargo con verbo, cargo como causa, error humano, nombres propios, documentos y culpa |
| `contact_data.yaml` | Correos, teléfonos y placas (A-45 punto 3), en claro y con evasiones Unicode |
| `standard_injection.yaml` | Instrucciones incrustadas en el texto de un estándar |
| `must_pass.yaml` | Límites que un falso positivo cruzaría: deben pasar |

Los caracteres invisibles, los espacios no ASCII y los homoglifos se escriben como escapes
`\uXXXX` dentro de comillas dobles, para que se vean al revisar. `test_laz_r5_corpus_validators.py`
lo ejecuta entero contra los dos validadores; TASK-316 lo repite contra la salida del asistente.

Reglas:

- **Solo datos generados.** Ningún caso sale de fotos, registros ni textos reales de una planta
  (carpetas `registros`): nombres, cargos, números y descripciones son sintéticos.
- Al menos 30 casos `reject` (NFR-LAZ-48); todo caso nuevo se queda como regresión permanente y
  sube la `version` de su archivo.
- El flujo de release repite el corpus si cambia el identificador del modelo configurado.
