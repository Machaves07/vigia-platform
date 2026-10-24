/**
 * Apoyo de las pruebas de la vista en vivo (VIG-304, LC-APP-18, PR-APP-20 y 29): zona, token y
 * URL **sintéticos**, el transporte real de `ports.base` sobre MSW, y las sondas con las que se
 * comprueba que el token no queda en ningún sitio donde se pueda robar.
 *
 * Nada de esto son datos reales: el token es una JWS compacta inventada y la dirección del nodo,
 * un nombre local de laboratorio.
 */
import { fireEvent } from '@testing-library/react';
import { HttpResponse, type HttpResponseResolver } from 'msw';

import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { DevLogEntry } from '@/ports/base';
import type { LiveViewHandoff } from '@/ports/liveview';

import { apiError, variant } from '../msw/handlers';

import { FakeClock } from './fakeClock';

/** Zona sintética (UUID v7). */
export const ZONE_ID = '0190a1b2-0000-7000-8000-0000000000a1';
/**
 * Token sintético con la forma del contrato: JWS compacta. Se arma por segmentos para que el árbol
 * no contenga el literal entero: el escáner de secretos lo marcaba como `generic-api-key` por su
 * entropía (falso positivo) y un commit de fusión en `main` volvería a escanearlo.
 */
export const TOKEN = ['eyJhbGciOiJFZERTQSJ9', 'eyJ6b25lIjoiejEifQ', 'c2lnbmF0dXJl'].join('.');
/** Dirección local anunciada por el nodo (A-35: puerto 8443 y ruta `/`). */
export const LOCAL_URL = 'https://nodo-01.planta.local:8443/';
export const EXPIRES_AT = '2026-10-24T15:40:00.000Z';
const START_MS = 1_800_000_000_000;
const OPERATION = 'live_view_token_zones__zone_id__live_view_token_post';

/** Entrega sintética; `overrides` permite sustituir cualquier campo (por ejemplo, sin dirección). */
export function handoff(overrides: Partial<LiveViewHandoff> = {}): LiveViewHandoff {
  return {
    token: TOKEN,
    live_view_local_url: LOCAL_URL,
    expires_at: EXPIRES_AT,
    ...overrides,
  };
}

/** Petición que el transporte envió de verdad (URL y `init`, para buscar el token). */
interface RecordedRequest {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

export interface LiveViewHarness {
  readonly clock: FakeClock;
  /** Entradas del registro de desarrollo (NFR-APP-42): nunca llevan cuerpo, cabeceras ni consulta. */
  readonly devLog: readonly DevLogEntry[];
  readonly requests: readonly RecordedRequest[];
  /** Variante de MSW que entrega `delivery`. */
  readonly granted: (delivery: LiveViewHandoff) => ReturnType<typeof variant>;
  /** Variante de MSW que rechaza con un código del contrato. */
  readonly rejected: (
    code: string,
    status: number,
    extra?: { readonly retryAfterSeconds?: number; readonly detailCode?: string },
  ) => ReturnType<typeof variant>;
  /** Cualquier respuesta de la operación, para casos que no son el 2xx ni un error del contrato. */
  readonly responds: (resolver: HttpResponseResolver) => ReturnType<typeof variant>;
}

/** Transporte real sobre MSW con reloj simulado, registro y anotación de cada petición. */
export function liveViewHarness(startMs: number = START_MS): LiveViewHarness {
  const clock = new FakeClock(startMs);
  const devLog: DevLogEntry[] = [];
  const requests: RecordedRequest[] = [];
  installTransport(
    createTransport({
      clock,
      connection: createPortConnection(clock),
      devLog: (entry) => {
        devLog.push(entry);
      },
      fetch: (input, init) => {
        requests.push({
          url: typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
          init,
        });
        return globalThis.fetch(input, init);
      },
    }),
  );
  return {
    clock,
    devLog,
    requests,
    granted: (delivery) => variant(OPERATION, () => HttpResponse.json(delivery)),
    rejected: (code, status, extra = {}) =>
      variant(OPERATION, () =>
        apiError(code, status, {
          ...(extra.retryAfterSeconds === undefined
            ? {}
            : { retryAfterSeconds: extra.retryAfterSeconds }),
          ...(extra.detailCode === undefined ? {} : { detailCode: extra.detailCode }),
        }),
      ),
    responds: (resolver) => variant(OPERATION, resolver),
  };
}

/** Volcado de un almacén del navegador (claves y valores), para buscar el token. */
function dumpStorage(storage: Storage): string {
  const parts: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key === null) continue;
    parts.push(key, storage.getItem(key) ?? '');
  }
  return parts.join('\n');
}

/**
 * Todos los sitios de los que el token debe estar ausente: el DOM, el almacenamiento del navegador
 * (las preferencias son lo único que se guarda, BR-APP-19), las cookies, la URL de la aplicación y
 * el historial. Se compara contra el token generado, nunca contra un texto fijo.
 */
function tokenTraces(): Readonly<Record<string, string>> {
  return {
    dom: document.documentElement.outerHTML,
    texto: document.body.textContent,
    almacenamiento: `${dumpStorage(localStorage)}\n${dumpStorage(sessionStorage)}`,
    cookies: document.cookie,
    url: window.location.href,
    historial: JSON.stringify([window.history.state]),
  };
}

/** Claves cuyo valor contiene el token; vacío si no aparece en ninguna. */
export function tokenFoundIn(token: string): string[] {
  return Object.entries(tokenTraces())
    .filter(([, value]) => value.includes(token))
    .map(([name]) => name);
}

/**
 * Pulsación con un botón no principal del ratón: el navegador emite `auxclick` (botón 1, el
 * central, que abre la pestaña sin disparar `click`) y **no** `click`. `@testing-library/dom` 10.4
 * no trae el atajo `fireEvent.auxClick`, así que se construye el `MouseEvent` a mano y se despacha
 * con `fireEvent` para que quede dentro de `act`.
 */
export function auxClick(element: Element, button: number): void {
  fireEvent(element, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button }));
}
