# Corpus de casos trampa de R5 (U-04)

Carpeta de los **30 casos trampa** del escenario de ataque R5 del plan de evaluación del PRD
(reidentificación por el asistente de causa raíz): LC-LAZ-43, `tests/abuse/`, y el validador de
atribución de TASK-340. La creó TASK-302 vacía para que las tareas de U-04 que la llenan no
choquen en archivos compartidos.

Reglas:

- **Solo datos generados.** Ningún caso sale de fotos, registros ni textos reales de una planta
  (carpetas `registros`): nombres, cargos y descripciones son sintéticos.
- Un caso por archivo, con su resultado esperado y la semilla con que se generó.
- El flujo de release repite el corpus si cambia el identificador del modelo configurado.
