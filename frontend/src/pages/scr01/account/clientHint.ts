/**
 * Resumen del navegador y del sistema de una sesión (VIG-366, «Personas, no sistemas»): el
 * servidor guarda el agente de usuario completo (`client_hint`) y la pantalla muestra «Chrome en
 * Windows», no la cadena entera. Lo que no se reconoce se dice como «otro navegador» u «otro
 * sistema», nunca con la cadena cruda.
 */

/** En orden: los que se anuncian también como otro (Edge y Opera dicen Chrome) van antes. */
const BROWSERS: readonly (readonly [RegExp, string])[] = [
  [/\bEdg(?:e|A|iOS)?\//, 'Edge'],
  [/\bOPR\/|\bOpera\b/, 'Opera'],
  [/\bFirefox\/|\bFxiOS\//, 'Firefox'],
  [/\bChrome\/|\bCriOS\/|\bChromium\//, 'Chrome'],
  [/\bVersion\/[\d.]+.*\bSafari\//, 'Safari'],
];

const SYSTEMS: readonly (readonly [RegExp, string])[] = [
  [/\bAndroid\b/, 'Android'],
  [/\biPad\b/, 'iPadOS'],
  [/\biPhone\b/, 'iOS'],
  [/\bWindows\b/, 'Windows'],
  [/\bMac OS X\b|\bMacintosh\b/, 'macOS'],
  [/\bCrOS\b/, 'ChromeOS'],
  [/\bLinux\b/, 'Linux'],
];

function first(table: readonly (readonly [RegExp, string])[], hint: string): string | null {
  return table.find(([pattern]) => pattern.test(hint))?.[1] ?? null;
}

/** Navegador y sistema reconocidos en el agente de usuario; `null` en cada uno si no se reconoce. */
export function clientSummary(hint: string): { browser: string | null; system: string | null } {
  return { browser: first(BROWSERS, hint), system: first(SYSTEMS, hint) };
}
