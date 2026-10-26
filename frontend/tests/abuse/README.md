# Escenarios permanentes de abuso (TASK-453 / VIG-335)

`VITEST_MAX_WORKERS=3 npm test -- tests/abuse` ejecuta A-1 a A-10 y BR-APP-32.
Solo se usan datos sintéticos. No se añaden dependencias.

- A-1, A-2, A-4 a A-9 usan componentes, sesión, caché y transporte reales con MSW.
  MSW representa respuestas del servidor; no sustituye las pruebas de autorización,
  RLS ni auditoría del backend (A-46, A-48, A-69).
- A-3 recorre el código y recibe por HTTP las cabeceras emitidas por
  `SecurityHeaders` de producción en un servidor efímero de Python estándar.
  Requiere `python3`, no instalar el backend. No comprueba el despliegue ni la
  aplicación de CSP por un navegador.
- A-10 fija los puntos de salida de red, rechaza nuevos recursos remotos en código,
  estilos y marcado inicial y comprueba las rutas relativas del catálogo OpenAPI.
  Las URL prefirmadas proceden del servidor; la vista local se abre por enlace.
  La política activa del navegador pertenece a VIG-336.
- BR-APP-32 recorre los puertos y el catálogo de operaciones, con una lista positiva
  de configuración/alta de nodos. Revocar o retirar una identidad administra la
  flota, no actúa sobre una máquina. Las rutas `/api/nodes/` son ingesta iniciada
  por el nodo y ningún puerto de pantalla puede usarlas.
- A-2 solo afirma la ausencia del token en la aplicación tras abrir; borrar el
  fragmento de la pestaña del nodo es responsabilidad de U-06 (D-1).
- A-7 comprueba las defensas del reproductor y la renovación al vencer, no promete
  impedir capturas del sistema operativo ni demostrar el vencimiento en S3.

## Mutaciones ejecutadas y restauradas

| Nombre                     | Cambio temporal                                                                  | Resultado real                                                                    |
| -------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `A05_person_filter`        | Añadir `Buscar por persona` con un `input type="search"` al formulario de SCR-13 | A-5: `expected ['SELECT', 'month', 'search'] to deeply equal ['SELECT', 'month']` |
| `A06_detail_periodic_60s`  | Cambiar la política del detalle de `audited` a `live`                            | A-6: `expected 6 to be 1` tras 5 minutos simulados                                |
| `A08_header_without_grant` | Enviar cabecera aun cuando la concesión es nula                                  | Los dos casos A-8 fallan: cabecera sintética en lugar de `null`                   |
| `A03_new_frame`            | Añadir un archivo nuevo con un `iframe`                                          | A-3: `expected ['ports/abuseProbe.tsx'] to deeply equal []`                       |
| `A03_no_frame_ancestors`   | Retirar la directiva del emisor de cabeceras real                                | A-3: la respuesta no contiene `frame-ancestors 'none'`                            |
| `A04_raw_markup`           | Sustituir el texto declarado por `dangerouslySetInnerHTML`                       | Los tres casos A-4 fallan; una entidad escapada se convierte en marcado           |
| `A07_download_enabled`     | Quitar `nodownload`                                                              | A-7: `expected [Array(2)] to include 'nodownload'`                                |
| `A10_external_destination` | Añadir `fetch` hacia un origen ajeno en un archivo nuevo                         | A-10: la URL externa coincide con el patrón prohibido                             |
| `BR32_machine_output`      | Añadir operación provisional `POST /machines/start`                              | BR-APP-32: la operación coincide con el patrón prohibido                          |

Las tres sondas de archivo nuevo se ejecutaron juntas; cada prueba cayó por su
propio vector. Todos los cambios temporales se retiraron antes de la corrida
completa en verde. A-1, A-2, A-9 y las demás aserciones de borde: **no verificado
con mutación**. No se añaden mecanismos de atomicidad ni concurrencia.
