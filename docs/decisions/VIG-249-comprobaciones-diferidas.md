# VIG-249 · Comprobaciones de despliegue diferidas a VIG-252

**Decide:** dueño del producto, 2026-10-22 (~10:35 UTC), comunicado por la sesión de
control en [la revisión del PR #192](https://github.com/Machaves07/vigia-platform/pull/192#issuecomment-6338650880).
Este archivo recoge esa decisión; no define los contratos de humo pendientes.

## Alcance autorizado

- Las comprobaciones nº 14 (rebote simulado → cola → usuario no entregable, sin nueva
  notificación) y nº 15 (exportación pequeña `ready`, verificador y hash reproducible)
  salen de VIG-249 / TASK-333 y pasan a VIG-252 / TASK-337, despliegue de U-04 del dueño.
- No se implementan aquí ni se cambia `notifications/registration.py`: la selección de
  `FakeEmailSender` fuera de `pilot` de TASK-325 sigue vigente.
- El dueño decidirá en VIG-252 cómo conciliar SES real en `staging-<n>` con esa selección,
  y qué significa reproducibilidad con el identificador y las marcas del `ExportJob`.

## Fallo cerrado obligatorio

Mientras falte ese contrato, `release.yml` declara ambas comprobaciones pendientes.
Con `VIGIA_AWS_ENABLED == 'true'`, el primer paso de `staging` y de `pilot` termina con
código 1 y el mensaje:

```text
comprobaciones nº 14 y 15 pendientes de contrato del dueño: VIG-252
```

No se despliegan esas pilas ni se publica la etiqueta. Se conserva la limpieza de staging
con `always()`. Sin AWS, el resumen explica el traslado y que no se ejecutaron ni se
superaron nº 14/15; el ensayo sin AWS de A-47 sigue disponible.

La guarda tiene pruebas que ejecutan el shell del YAML y se comprueban retirándola y
convirtiendo su salida en éxito. Este traslado y la guarda sustituyen el criterio 4
original de TASK-333, según la decisión citada. La sesión de control actualiza el archivo
de tarea del repositorio de planificación, fuera de este workspace.

## Nightly y nombres

Se relanza nightly sobre la rama sin `update_baseline`. Si vuelve a fallar
`gob_node_observability_event`, se registra la cifra sin tocar bases ni factores.
El nombre del trabajo `bancos` menciona U-04; los nombres de los checks de `ci.yml`
se conservan exactamente. La auditoría independiente y el CI siguen siendo obligatorios;
esta decisión no autoriza al agente a fusionar ni a ejecutar AWS.
