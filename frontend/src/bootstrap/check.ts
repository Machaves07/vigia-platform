/**
 * LC-APP-01 `app.bootstrap` (PAT-APP-REN-01 y RES-07; NFR-APP-31; PR-APP-26).
 *
 * `index.html` carga este módulo como guion propio externo —sin código en línea— antes que nada:
 *
 * 1. comprueba las cinco capacidades de la política de navegadores: `fetch`, `Intl.DateTimeFormat`
 *    con zonas horarias, `crypto.subtle.digest` (SHA-256 de PAT-APP-SEG-02), `video` con H.264
 *    (R-APP-2) y la versión del navegador frente a la mínima derivada de `browserslist` —que cubre
 *    `Sec-Fetch-Site`, del que depende la barrera anti-falsificación de U-02—;
 * 2. si falta alguna, deja visible la sección estática «navegador no compatible» de `index.html`
 *    (quitar `hidden`, nada más: su texto va compilado en el marcado) y termina; **React no se
 *    carga**;
 * 3. si están todas, carga el paquete inicial (`src/main.tsx`), que arranca el paso 3 (VIG-320).
 *
 * Contrato de LC-APP-01: no hace peticiones —autenticadas ni de ningún otro tipo— y no accede al
 * almacenamiento. Un navegador que no se reconozca o que no esté en la política se trata como no
 * compatible (fallo cerrado, PAT-APP-RES-07).
 */
import { MIN_BROWSER_VERSIONS } from './minVersions.generated';

/** Contenedor de la aplicación en `index.html` (R-APP-7). */
export const APP_CONTAINER_ID = 'vigia-app';

/** Sección estática «navegador no compatible» de `index.html`, oculta al cargar. */
export const UNSUPPORTED_SECTION_ID = 'vigia-unsupported';

/** Las capacidades de NFR-APP-31, en orden fijo. */
export const CAPABILITIES = [
  'fetch',
  'intl_timezone',
  'crypto_subtle',
  'video_h264',
  'browser_version',
] as const;

export type CapabilityId = (typeof CAPABILITIES)[number];

/** Motor de la política de NFR-APP-31: las claves de la tabla generada. */
export type BrowserEngine = keyof typeof MIN_BROWSER_VERSIONS;

/** Zona horaria de la comprobación: un motor sin datos de zonas no la resuelve. */
const ZONE_PROBE = 'America/Bogota';

/** Tipo exacto del diseño para el clip de H.264 (R-APP-2). */
export const H264_TYPE = 'video/mp4; codecs="avc1"';

/** Lo que se usa de `Intl.DateTimeFormat`: construir con `timeZone` y leer la zona resuelta. */
export interface DateTimeFormatLike {
  resolvedOptions(): { readonly timeZone?: string | undefined };
}

export type DateTimeFormatConstructor = new (
  locale: string,
  options: { readonly timeZone: string },
) => DateTimeFormatLike;

/** Superficie del navegador que comprueba el arranque; inyectable para probarla entera. */
export interface CapabilityProbe {
  /** `globalThis.fetch` (ausente en navegadores antiguos). */
  readonly fetch: unknown;
  /** `Intl.DateTimeFormat`, o `undefined` si el navegador no tiene `Intl`. */
  readonly dateTimeFormat: DateTimeFormatConstructor | undefined;
  /** `crypto.subtle`; solo existe en contexto seguro. */
  readonly subtle: { readonly digest?: unknown } | undefined;
  /**
   * `HTMLMediaElement.prototype.canPlayType` invocado sobre un elemento `video` nuevo, o
   * `undefined`. El método nativo exige un `HTMLMediaElement` como receptor: sin él lanza
   * `TypeError` (`Illegal invocation`) y el vídeo quedaría ausente en cualquier navegador (VIG-342).
   */
  readonly canPlayType: ((type: string) => string) | undefined;
  /** `navigator.userAgent`; vacío si el navegador no lo expone. */
  readonly userAgent: string;
}

/** Lo que se lee del navegador para construir la sonda. */
export interface BrowserEnvironment {
  readonly fetch?: unknown;
  readonly Intl?: { readonly DateTimeFormat?: unknown } | undefined;
  readonly crypto?: { readonly subtle?: { readonly digest?: unknown } | undefined } | undefined;
  readonly HTMLMediaElement?:
    { readonly prototype: { readonly canPlayType?: unknown } } | undefined;
  /** El documento que crea el elemento `video` receptor de `canPlayType`. */
  readonly document?: { readonly createElement?: (tagName: 'video') => unknown } | undefined;
  readonly navigator?: { readonly userAgent?: string } | undefined;
}

/** La superficie del navegador en marcha; en un entorno sin ella, todo queda ausente. */
export function currentEnvironment(): BrowserEnvironment {
  return {
    fetch: globalThis.fetch,
    Intl: globalThis.Intl,
    crypto: globalThis.crypto,
    HTMLMediaElement: globalThis.HTMLMediaElement,
    document: globalThis.document,
    navigator: globalThis.navigator,
  };
}

/**
 * `canPlayType` del prototipo, invocado sobre un `video` que crea el documento en cada llamada.
 * Los dos métodos nativos se llaman con su receptor explícito (el documento y el elemento): sin él
 * lanzan. Lo que falle al crear el elemento o al preguntar lo recoge `supportsH264` como capacidad
 * ausente, y un receptor que no sea un `HTMLMediaElement` hace lanzar al método (fallo cerrado).
 */
function h264Probe(environment: BrowserEnvironment): ((type: string) => string) | undefined {
  const canPlayType = environment.HTMLMediaElement?.prototype.canPlayType;
  const page = environment.document;
  const createElement = page?.createElement;
  if (typeof canPlayType !== 'function' || typeof createElement !== 'function') return undefined;
  return (type) => {
    const video: unknown = Reflect.apply(createElement, page, ['video']);
    const verdict: unknown = Reflect.apply(canPlayType, video, [type]);
    return typeof verdict === 'string' ? verdict : '';
  };
}

/** Sonda de `environment` (por omisión, el navegador en marcha). */
export function browserProbe(
  environment: BrowserEnvironment = currentEnvironment(),
): CapabilityProbe {
  const dateTimeFormat = environment.Intl?.DateTimeFormat;
  return {
    fetch: environment.fetch,
    dateTimeFormat:
      typeof dateTimeFormat === 'function'
        ? (dateTimeFormat as DateTimeFormatConstructor)
        : undefined,
    subtle: environment.crypto?.subtle,
    canPlayType: h264Probe(environment),
    userAgent: environment.navigator?.userAgent ?? '',
  };
}

/** `Intl.DateTimeFormat` resuelve una zona con nombre (y no otra): hay datos de zonas horarias. */
export function supportsTimeZones(dateTimeFormat: DateTimeFormatConstructor | undefined): boolean {
  if (dateTimeFormat === undefined) return false;
  try {
    const resolved = new dateTimeFormat('es', { timeZone: ZONE_PROBE }).resolvedOptions().timeZone;
    return resolved === ZONE_PROBE;
  } catch {
    return false;
  }
}

/** El navegador declara poder reproducir el tipo exacto del diseño (R-APP-2). */
export function supportsH264(canPlayType: ((type: string) => string) | undefined): boolean {
  if (canPlayType === undefined) return false;
  try {
    const verdict = canPlayType(H264_TYPE);
    return verdict === 'maybe' || verdict === 'probably';
  } catch {
    return false;
  }
}

/** Motor y versión del navegador en marcha. */
export interface DetectedBrowser {
  readonly engine: BrowserEngine;
  readonly major: number;
  readonly minor: number;
}

/**
 * Marcas de navegadores fuera de la política de NFR-APP-31: ni Internet Explorer ni navegadores
 * integrados en aplicaciones, ni familias que comparten motor con las de la política y se
 * confundirían con ellas al mirar la cadena de agente.
 */
const OUT_OF_POLICY = [
  'MSIE',
  'Trident/',
  'Edge/',
  'OPR/',
  'Opera',
  'SamsungBrowser',
  'YaBrowser',
  'UCBrowser',
  'MiuiBrowser',
  'FBAN',
  'FBAV',
  'Instagram',
  'MicroMessenger',
  'Line/',
  '; wv)',
  'Electron/',
  'CriOS',
  'FxiOS',
  'EdgiOS',
] as const;

/** Motor y versión en la cadena de agente; el orden importa (Edge y Chrome llevan `Safari/`). */
const ENGINES: readonly (readonly [BrowserEngine, RegExp])[] = [
  ['edge', / Edg(?:e|A)?\/(\d+)(?:\.(\d+))?/],
  ['chrome', / (?:Chrome|Chromium)\/(\d+)(?:\.(\d+))?/],
  ['firefox', / Firefox\/(\d+)(?:\.(\d+))?/],
  ['safari', / Version\/(\d+)(?:\.(\d+))?[ .].*Safari\//],
];

/** Navegador de la cadena de agente, o `null` si no es de la política (fallo cerrado). */
export function detectBrowser(userAgent: string): DetectedBrowser | null {
  if (OUT_OF_POLICY.some((marker) => userAgent.includes(marker))) return null;
  for (const [engine, pattern] of ENGINES) {
    const match = pattern.exec(userAgent);
    if (match === null) continue;
    const major = Number(match[1]);
    const minor = match[2] === undefined ? 0 : Number(match[2]);
    if (!Number.isSafeInteger(major) || !Number.isSafeInteger(minor)) return null;
    return { engine, major, minor };
  }
  return null;
}

/** La versión del navegador alcanza la mínima que fija `browserslist` para su motor. */
export function supportsBrowserVersion(userAgent: string): boolean {
  const detected = detectBrowser(userAgent);
  if (detected === null) return false;
  const minimum = MIN_BROWSER_VERSIONS[detected.engine];
  return (
    detected.major > minimum[0] || (detected.major === minimum[0] && detected.minor >= minimum[1])
  );
}

/** Las capacidades que faltan, en el orden de `CAPABILITIES`; vacío si están todas. */
export function missingCapabilities(probe: CapabilityProbe): readonly CapabilityId[] {
  const missing: CapabilityId[] = [];
  if (typeof probe.fetch !== 'function') missing.push('fetch');
  if (!supportsTimeZones(probe.dateTimeFormat)) missing.push('intl_timezone');
  if (typeof probe.subtle?.digest !== 'function') missing.push('crypto_subtle');
  if (!supportsH264(probe.canPlayType)) missing.push('video_h264');
  if (!supportsBrowserVersion(probe.userAgent)) missing.push('browser_version');
  return missing;
}

/** Deja visible la sección estática: quita `hidden` y nada más (LC-APP-01). `false` si no está. */
export function revealUnsupported(document: Document): boolean {
  const section = document.getElementById(UNSUPPORTED_SECTION_ID);
  if (section === null) return false;
  section.removeAttribute('hidden');
  return true;
}

export interface BootstrapOptions {
  /** Sonda de capacidades; por omisión, la del navegador en marcha. */
  readonly probe?: CapabilityProbe;
  /** Documento de `index.html`. */
  readonly document: Document;
  /** Carga del paquete inicial: `() => import('../main')`. */
  readonly loadApp: () => Promise<unknown>;
}

/**
 * Arranque de LC-APP-01. Devuelve la promesa de la carga del paquete inicial **si y solo si**
 * están todas las capacidades; si falta alguna, deja visible la sección estática y devuelve
 * `null`. La decisión es síncrona —en el mismo turno ya se sabe si React se carga— y no se hace
 * ninguna petición ni se toca el almacenamiento.
 */
export function start(options: BootstrapOptions): Promise<unknown> | null {
  const missing = missingCapabilities(options.probe ?? browserProbe());
  if (missing.length > 0) {
    revealUnsupported(options.document);
    return null;
  }
  return options.loadApp();
}

// `index.html` carga este módulo como guion propio: sin contenedor de la aplicación no hay página
// que arrancar (las pruebas importan el módulo y llaman a `start` con su propia sonda).
if (document.getElementById(APP_CONTAINER_ID) !== null) {
  void start({ document, loadApp: () => import('../main') });
}
