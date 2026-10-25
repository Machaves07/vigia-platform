/**
 * Apoyo de las pruebas del arranque (VIG-289, PR-APP-26): la página real de `index.html` y sondas
 * sintéticas con exactamente las capacidades que se pidan, para recorrer todos los subconjuntos de
 * NFR-APP-31. Sin datos de personas ni de organizaciones.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  browserProbe,
  UNSUPPORTED_SECTION_ID,
  type BrowserEnvironment,
  type CapabilityId,
  type CapabilityProbe,
} from '@/bootstrap/check';

/**
 * `index.html` del repositorio, tal como lo sirve la construcción sin transformar.
 *
 * La ruta sale de `import.meta.url` sin `new URL(…, import.meta.url)`: Vite reescribe esa forma
 * como una URL del servidor de desarrollo, y en jsdom deja de ser `file:`.
 */
const INDEX_HTML = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'index.html'),
  'utf8',
);

export interface BootstrapPage {
  readonly document: Document;
  readonly section: Element;
}

/** La página de `index.html` en un documento aparte, con su sección de no compatible oculta. */
export function bootstrapPage(): BootstrapPage {
  const document = new DOMParser().parseFromString(INDEX_HTML, 'text/html');
  const section = document.getElementById(UNSUPPORTED_SECTION_ID);
  if (section === null) {
    throw new Error(`index.html: falta la sección #${UNSUPPORTED_SECTION_ID}.`);
  }
  return { document, section };
}

/** Chrome por encima del mínimo que fija la política de navegadores. */
const CURRENT_CHROME_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/156.0.0.0 Safari/537.36';

/** Chrome por debajo del mínimo: la capacidad de versión falta. */
const OLD_CHROME_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/100.0.0.0 Safari/537.36';

/** `Intl.DateTimeFormat` que ignora `timeZone`: un motor sin datos de zonas horarias. */
export class DateTimeFormatWithoutZones {
  resolvedOptions(): { readonly timeZone?: string | undefined } {
    return {};
  }
}

/**
 * `HTMLMediaElement` sintético que se comporta como el nativo (VIG-342): su `canPlayType` lanza
 * `TypeError` («Illegal invocation») si el receptor no es uno de sus elementos y, si lo es,
 * responde `verdict`. El documento crea esos elementos.
 */
export function mediaEnvironment(
  verdict: string,
): Required<Pick<BrowserEnvironment, 'HTMLMediaElement' | 'document'>> {
  class SyntheticMediaElement {
    canPlayType(): string {
      if (!(this instanceof SyntheticMediaElement)) throw new TypeError('Illegal invocation');
      return verdict;
    }
  }
  return {
    HTMLMediaElement: SyntheticMediaElement,
    document: { createElement: () => new SyntheticMediaElement() },
  };
}

/**
 * Sonda con exactamente las capacidades de `present`: las ausentes quedan como las de un navegador
 * que no las tiene (`fetch` y `crypto.subtle` sin definir, `canPlayType` que declara que no, un
 * agente por debajo del mínimo).
 */
export function probeWith(present: readonly CapabilityId[]): CapabilityProbe {
  const has = (id: CapabilityId) => present.includes(id);
  return browserProbe({
    fetch: has('fetch') ? () => undefined : undefined,
    Intl: has('intl_timezone') ? { DateTimeFormat: Intl.DateTimeFormat } : undefined,
    crypto: has('crypto_subtle') ? { subtle: { digest: () => undefined } } : undefined,
    ...mediaEnvironment(has('video_h264') ? 'probably' : ''),
    navigator: { userAgent: has('browser_version') ? CURRENT_CHROME_UA : OLD_CHROME_UA },
  });
}
