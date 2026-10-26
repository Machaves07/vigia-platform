# U-05: revisión manual de accesibilidad antes del piloto

Lista de la revisión manual de NFR-APP-50 que hace el dueño en VIG-338 (TASK-460). La
verificación automática ya cubre las dieciséis pantallas, pero no sustituye a una persona con
teclado y lector de pantalla:

- `tests/quality/a11y_all_screens.test.tsx` pasa `axe` sobre cada grupo de rutas de `SCREENS`
  con los datos de MSW, en jsdom. Sin violaciones serias ni críticas.
- `tests/e2e/a11y.spec.ts` pasa `@axe-core/playwright` (WCAG 2.1 A y AA) en Chromium, Firefox y
  WebKit contra el mundo local. Ahí sí cuentan el contraste y el tamaño de los objetivos.

Ninguna herramienta automática comprueba si el orden de lectura tiene sentido, si un anuncio
llega en el momento justo o si se puede terminar una tarea sin ratón. Eso es lo que revisa esta
lista.

## Preparación

- Construcción de producción servida por `vigia-api` (mundo local de `tests/e2e/README.md` o
  `staging` con la organización sintética). Nunca datos ni fotos reales.
- Escritorio: Chromium o Edge con NVDA (Windows); Safari con VoiceOver (macOS).
- Tableta (SCR-16 de COPASST): Safari con VoiceOver (iPadOS) o Chrome con TalkBack (Android).
- Zoom del navegador al 200 % en al menos una pasada por pantalla.
- Anote cada hallazgo con la pantalla, el paso, el navegador, el lector y lo que esperaba oír o
  ver. No copie datos de la pantalla en la nota: basta con el identificador sintético.

## Comprobaciones comunes (todas las pantallas revisadas)

- [ ] El documento se anuncia en español (`lang="es"`) y el lector pronuncia bien los textos.
- [ ] El primer `Tab` lleva a la navegación principal o al contenido. El foco siempre se ve,
      también sobre botones de tabla, casillas y enlaces dentro de avisos.
- [ ] El orden del foco sigue el orden visual. Ningún control queda inalcanzable ni atrapa el
      foco. `Esc` cierra lo que se abrió y devuelve el foco al control que lo abrió.
- [ ] Cada pantalla tiene un único `h1` con su nombre y los encabezados no saltan niveles.
- [ ] El semáforo de observabilidad y todo estado con color también se anuncian con texto
      («Observable», «Degradada», «No observable»). Nunca hay solo color (BR-APP-35).
- [ ] Los avisos de error, de sesión y de conexión se anuncian una vez, sin robar el foco, y el
      código de correlación se puede leer y copiar con teclado.
- [ ] Los campos con error anuncian el mensaje anclado al campo. El foco va al primer error al
      enviar.
- [ ] Al 200 % no hay desplazamiento horizontal en el contenido principal ni texto cortado.
- [ ] Los indicadores «Actualizado…» y «Actualizar» se entienden sin ver la pantalla.

## SCR-08: inicio, bandeja y detalle del hallazgo

- [ ] Inicio del coordinador (`/home`): las regiones «Observabilidad por zona» y «Hallazgos
      nuevos» se recorren como regiones con nombre. Cada zona anuncia su estado con texto.
- [ ] Bandeja (`/findings`): los filtros se cambian con teclado sin lanzar consultas. Solo
      «Aplicar» consulta, y el resultado (o «sin resultados») se anuncia.
- [ ] La tabla de la bandeja anuncia los encabezados de columna al moverse por celdas. El enlace
      de cada fila dice a qué hallazgo lleva sin depender del color ni de iconos.
- [ ] Paginación por cursor: «Siguiente» y «Anterior» se alcanzan con teclado y el lector
      anuncia el cambio de página.
- [ ] Detalle (`/findings/:id`): el aviso de lectura auditada se lee antes de consultar. El
      reproductor del clip difuminado se controla con teclado (reproducir, pausar, buscar) y su
      estado se anuncia.
- [ ] La marca «agregado en el tiempo» y la línea de cobertura se entienden con el lector, sin
      depender del gráfico.

## SCR-09: clasificación y lazo de cierre

- [ ] Clasificación (`/findings/:id/classify`): el grupo de resultados de clasificación se
      recorre con flechas y cada opción se anuncia con su etiqueta en español.
- [ ] El campo de motivo anuncia su límite y lo que queda. El rechazo por texto no admitido llega
      al campo y no se pierde lo escrito.
- [ ] El asistente de pasos (lazo, evidencia y cierre) anuncia «Paso N de M» y el nombre del
      paso. «Atrás» y «Siguiente» conservan lo escrito.
- [ ] Subida de adjunto: la casilla «no contiene ninguna persona identificable» se marca con
      teclado. El progreso y el resultado de la subida se anuncian.
- [ ] Bloque de firma: queda claro qué se firma, quién firma y que la firma es una acción humana
      separada. La confirmación se anuncia y el foco no salta a otra parte.
- [ ] «SIN ALTERNATIVA SEGURA DISPONIBLE» y la reapertura con acción nueva se leen completas,
      con su motivo, sin abreviaturas.

## SCR-11: causa raíz

- [ ] Los cinco porqués se recorren en orden. Cada uno anuncia su número, su categoría
      (proceso, ingeniería u organización) y si ya está guardado en el servidor.
- [ ] El panel del asistente anuncia que su texto es un borrador y no una decisión. Aceptar,
      editar o descartar el borrador se hace con teclado, y el estado resultante se anuncia.
- [ ] El contexto objetivo de la zona (cobertura, observabilidad) se lee como texto, sin depender
      de gráficos.
- [ ] La firma del análisis está separada del guardado y se anuncia como acción humana.

## SCR-16: transparencia (escritorio y tableta)

- [ ] Lista de zonas (`/transparency`): cada zona se alcanza con teclado o con gestos del lector
      en tableta y su nombre basta para elegirla.
- [ ] Vista de la zona (`/transparency/:id`): «Alcance declarado», «Estándares declarados»,
      «Acuerdo de uso» y «Compuertas de la zona» se recorren como encabezados. Ningún dato de
      hallazgos ni de personas se anuncia.
- [ ] «Confirmar mi firma» y «Abrir vista en vivo» tienen al menos 44 × 44 px en tableta y el
      lector los anuncia como botones con ese nombre.
- [ ] La confirmación de la firma se anuncia con su acuse (fecha y rol), sin mover el foco a otro
      sitio.
- [ ] El enlace de la vista en vivo dice que abre una pestaña nueva hacia el nodo local y
      desaparece al pulsarlo o al vencer; la cuenta atrás no interrumpe al lector cada segundo.

## Cierre

- [ ] Hallazgos serios (una tarea que no se puede terminar con teclado o lector): issue de
      seguimiento de VIG-338, según la regla de issues que bloquean.
- [ ] Hallazgos menores: en el comentario de VIG-338, con pantalla y paso.
- [ ] Revisión firmada por el dueño con fecha, navegador y lector usados.
