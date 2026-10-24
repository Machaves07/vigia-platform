/**
 * Modo `VITE_API_MODE=mock` (`src/mocks/browser.ts`, PAT-APP-MAN-02): `fetch` a una operación de
 * `app.yaml` lo responde su manejador generado; lo demás sigue a la red; `stop()` lo deshace.
 */
import { describe, expect, it, vi } from 'vitest';

import { startMockApi } from '@/mocks/browser';

import { apiOperation, successStatus } from '../../generators';
import { handlersByOperation, MOCK_OPERATION_HEADER } from '../../msw/handlers';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

/** Página falsa en el origen de jsdom, contra el que MSW resuelve las rutas relativas. */
const ORIGIN = window.location.origin;

function target() {
  const network = vi.fn<typeof fetch>(() => Promise.resolve(new Response('red', { status: 299 })));
  return {
    network,
    target: { fetch: network as typeof fetch, location: { href: `${ORIGIN}/panel` } },
  };
}

describe('startMockApi', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('responde una operación con su manejador, también con ruta relativa', async () => {
    const { network, target: page } = target();
    const api = startMockApi(page);
    const [operationId] = [...handlersByOperation.keys()].filter(
      (id) => apiOperation(id).path === '/me',
    );
    if (operationId === undefined) throw new Error('app.yaml sin /me');
    const response = await page.fetch('/me');
    expect(response.status).toBe(Number(successStatus(apiOperation(operationId))));
    expect(response.headers.get(MOCK_OPERATION_HEADER)).toBe(operationId);
    expect(await response.json()).toHaveProperty('effective_permissions');
    expect(network).not.toHaveBeenCalled();
    api.stop();
  });

  it('acepta Request y URL, y deja pasar a la red lo que no es de la API', async () => {
    const { network, target: page } = target();
    const api = startMockApi(page);
    const viaRequest = await page.fetch(new Request(`${ORIGIN}/health/live`));
    expect(viaRequest.status).toBe(200);
    const other = await page.fetch(new URL(`${ORIGIN}/assets/logo.svg`));
    expect(other.status).toBe(299);
    expect(network).toHaveBeenCalledTimes(1);
    const forwarded = network.mock.calls[0]?.[0];
    expect(forwarded instanceof Request ? forwarded.url : String(forwarded)).toBe(
      `${ORIGIN}/assets/logo.svg`,
    );
    api.stop();
  });

  it('stop() devuelve fetch a la red y no pisa un fetch posterior', async () => {
    const { network, target: page } = target();
    const api = startMockApi(page);
    api.stop();
    expect(page.fetch).toBe(network);
    await page.fetch('/me');
    expect(network).toHaveBeenCalledTimes(1);
    const again = startMockApi(page);
    const later = vi.fn<typeof fetch>();
    page.fetch = later;
    again.stop();
    expect(page.fetch).toBe(later);
  });
});
