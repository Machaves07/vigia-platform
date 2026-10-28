/**
 * Tipografía propia (sistema visual v1, VIG-364): las caras de IBM Plex se piden todas al arrancar.
 *
 * El navegador solo descarga una cara cuando la usa el texto pintado: un peso que aparece más
 * tarde (un botón en negrita, un formulario nuevo) cambiaría la caja de su texto en mitad de un
 * gesto y movería lo que hay debajo. Pedirlas al arrancar deja el cambio en la primera pintura.
 * Un fallo de carga deja la fuente del sistema de `--font-family`; nunca rompe la aplicación.
 */
export function preloadFonts(fonts: Iterable<FontFace> = document.fonts): void {
  for (const face of fonts) {
    if (face.status === 'unloaded') void face.load().catch(() => undefined);
  }
}
