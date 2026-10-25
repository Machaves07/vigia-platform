/**
 * FS-APP-09 (PAT-APP-RES-03 y SEG-03, BR-APP-65 y 66): la vista en vivo local es inalcanzable
 * desde fuera de la planta.
 *
 * El lanzador de la vista en vivo dentro de una pantalla. La pestaña del nodo no carga (la
 * persona no está en la red de la planta), cosa que la aplicación no puede detectar: por eso la
 * pantalla previa ya explicó que solo funciona en esa red y qué hacer si no carga. «Abrir de
 * nuevo» pide **otro** token cada vez; la aplicación nunca llama al nodo, no retiene ningún token
 * usado y el resto de la pantalla, la conexión y los circuitos siguen igual.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';

import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { installTransport } from '@/ports/base';
import { text } from '@/texts';
import catalog from '@/texts/live_view.json';

import { server } from '../setup/msw';
import { handoff, liveViewHarness, LOCAL_URL, tokenFoundIn, ZONE_ID } from '../support/liveView';
import { integer, scenarioCase, settle, traffic } from './harness';

const WAIT = { timeout: 10_000 };
const EXPLANATION = [
  'live_view.local_network',
  'live_view.if_not_loading',
  'live_view.open_again_token',
] as const;

afterEach(() => {
  installTransport(null);
});

/** JWS compacta sintética armada al ejecutar (un literal con esa forma lo marca el escáner). */
function syntheticToken(serial: number, salt: string): string {
  const segment = (value: string) =>
    btoa(value).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
  return [segment('{"alg":"EdDSA"}'), segment(`{"n":${String(serial)}}`), `firma-${salt}`].join(
    '.',
  );
}

function launcher(): HTMLElement {
  return screen.getByRole('region', { name: text('live_view.region_label') });
}

function requestButton(): HTMLButtonElement {
  const button = launcher().querySelector('[data-part="request"]');
  if (!(button instanceof HTMLButtonElement)) throw new Error('Falta el botón de abrir.');
  return button;
}

describe('FS-APP-09 · vista en vivo local inalcanzable', { timeout: 120_000 }, () => {
  it('pantalla previa explicativa; «abrir de nuevo» pide otro token; sin efecto en la aplicación', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-09',
      fc.record({
        reopenings: integer(1, 3),
        salt: fc.stringMatching(/^[a-z0-9]{6}$/),
      }),
    );
    const harness = liveViewHarness();
    const tokens: string[] = [];
    server.use(
      harness.responds(() => {
        const token = syntheticToken(tokens.length + 1, parameters.salt);
        tokens.push(token);
        return HttpResponse.json(handoff({ token }));
      }),
    );
    const log = traffic();
    const { container } = render(
      <main>
        <h1>Pantalla sintética</h1>
        <LiveViewLauncher zone_id={ZONE_ID} clock={harness.clock} />
        <p data-part="rest">Resto de la pantalla</p>
      </main>,
    );

    // Antes de pedir nada, la pantalla previa explica la red de la planta y qué hacer si no carga.
    for (const key of EXPLANATION) {
      expect(container.querySelector(`[data-text-key="${key}"]`)?.textContent).toBe(catalog[key]);
    }

    for (let opening = 0; opening <= parameters.reopenings; opening += 1) {
      expect(requestButton().textContent).toBe(
        text(opening === 0 ? 'live_view.open' : 'live_view.open_again'),
      );
      fireEvent.click(requestButton());
      const link = await screen.findByRole<HTMLAnchorElement>(
        'link',
        { name: text('live_view.link') },
        WAIT,
      );
      const token = tokens[opening] ?? '';
      expect(new URL(link.href).origin).toBe(new URL(LOCAL_URL).origin);
      expect(new URL(link.href).hash).toBe(`#token=${token}`);
      // Se abre la pestaña del nodo, que no carga: el enlace sale del DOM con su token.
      fireEvent.click(link);
      await settle();
      expect(launcher().querySelector('[data-part="link"]')).toBeNull();
      expect(tokenFoundIn(token)).toEqual([]);
    }

    // Un token por apertura, todos distintos; nada fue al nodo ni a otro origen.
    expect(log.count('POST', `/zones/${ZONE_ID}/live-view-token`)).toBe(parameters.reopenings + 1);
    expect(new Set(tokens).size).toBe(parameters.reopenings + 1);
    expect(log.entries).toHaveLength(parameters.reopenings + 1);
    expect(harness.requests.every((request) => !request.url.startsWith(LOCAL_URL))).toBe(true);
    expect(screen.getByRole('heading', { name: 'Pantalla sintética' })).toBeDefined();
    expect(container.querySelector('[data-part="rest"]')?.textContent).toBe('Resto de la pantalla');
    for (const key of EXPLANATION) {
      expect(container.querySelector(`[data-text-key="${key}"]`)?.textContent).toBe(catalog[key]);
    }
  });
});
