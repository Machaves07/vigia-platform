/**
 * PR-APP-29 (PAT-APP-SEG-03, BR-APP-65, R-APP-4): tras el clic (también el central), el vencimiento
 * o cualquiera de los caminos de apertura, el token no existe en el DOM, la memoria del componente,
 * el almacenamiento, el historial ni los registros; el enlace no tiene `opener` y no envía
 * referente; y `window.open` nunca se llama.
 *
 * La memoria del componente es el estado del reductor de UI-18: la primera propiedad recorre
 * secuencias de eventos y comprueba que, fuera de `link`, no queda ni rastro del token. La segunda
 * recorre la interfaz de verdad (MSW y el transporte real) y sondea el DOM, el almacenamiento, las
 * cookies, la URL, el historial, el registro de desarrollo y la consola.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse } from 'msw';
import { createElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import {
  initialLiveView,
  liveViewReducer,
  type LiveViewEvent,
  type LiveViewState,
} from '@/components/LiveViewLauncher/state';
import { installTransport } from '@/ports/base';
import type { LiveViewHandoff } from '@/ports/liveview';
import { LIVE_VIEW_TOKEN_SECONDS } from '@/state/reducers/countdown';
import { text } from '@/texts';

import { liveViewDelivery } from '../generators';
import { server } from '../setup/msw';
import { liveViewHarness, auxClick, tokenFoundIn, ZONE_ID } from '../support/liveView';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const START_MS = 1_800_000_000_000;
const REJECTED = { code: 'rate_limited', status: 429, retry_after_seconds: 30 } as const;
/** Caminos de apertura: clic normal, botón central del ratón (sin `click`) y vencimiento. */
const OPENING = fc.constantFrom('click', 'middle', 'expire');

afterEach(() => {
  installTransport(null);
  localStorage.clear();
  sessionStorage.clear();
});

/** El token solo puede estar en `href` y solo mientras el enlace vive (R-APP-4). */
function expectTokenOnlyInLiveLink(state: LiveViewState, token: string): void {
  const copies = JSON.stringify(state).split(token).length - 1;
  if (state.phase === 'link') {
    expect(state.href).toContain(token);
    expect(copies).toBe(1);
  } else {
    expect(state.href).toBeNull();
    expect(copies).toBe(0);
  }
}

function eventFor(kind: string, delivery: LiveViewHandoff, nowMs: number): LiveViewEvent {
  switch (kind) {
    case 'request':
      return { type: 'request', nowMs };
    case 'granted':
      return { type: 'granted', handoff: delivery, nowMs };
    case 'rejected':
      return { type: 'rejected', error: REJECTED, nowMs };
    case 'opened':
      return { type: 'opened' };
    case 'dismiss':
      return { type: 'dismiss' };
    default:
      return { type: 'tick', nowMs };
  }
}

function clickRequest(): void {
  const button = document.querySelector<HTMLButtonElement>('[data-part="request"]');
  if (button === null) throw new Error('Falta el botón de abrir la vista en vivo.');
  fireEvent.click(button);
}

describe('PR-APP-29 · el token no sobrevive al clic ni al vencimiento', () => {
  it(
    'en toda secuencia de eventos, fuera del enlace vivo no queda rastro del token',
    () => {
      const kinds = fc.constantFrom('request', 'granted', 'rejected', 'opened', 'dismiss', 'tick');
      const steps = fc.array(fc.tuple(kinds, fc.integer({ min: 0, max: 700_000 })), {
        maxLength: 20,
      });
      fc.assert(
        fc.property(liveViewDelivery(), steps, (delivery, sequence) => {
          const token = delivery.token as string;
          const handoff = delivery as unknown as LiveViewHandoff;
          let state = initialLiveView;
          let nowMs = START_MS;
          for (const [kind, step] of sequence) {
            nowMs += step;
            state = liveViewReducer(state, eventFor(kind, handoff, nowMs));
            expect(state.href === null).toBe(state.phase !== 'link');
            expectTokenOnlyInLiveLink(state, token);
          }
          // Al vencer la cuenta, el enlace se cierra aunque no llegue ningún evento más.
          state = liveViewReducer(state, {
            type: 'tick',
            nowMs: nowMs + LIVE_VIEW_TOKEN_SECONDS * 1000 + 1,
          });
          expectTokenOnlyInLiveLink(state, token);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'tras el clic (o el clic central) o el vencimiento el token sale del DOM, el almacenamiento, el historial y los registros',
    async () => {
      const harness = liveViewHarness();
      const open = vi.spyOn(window, 'open').mockImplementation(() => null);
      const consoles = [
        vi.spyOn(console, 'log'),
        vi.spyOn(console, 'info'),
        vi.spyOn(console, 'warn'),
        vi.spyOn(console, 'error'),
        vi.spyOn(console, 'debug'),
      ];
      await fc.assert(
        fc.asyncProperty(liveViewDelivery(), OPENING, async (delivery, opening) => {
          const token = delivery.token as string;
          const fromRequests = harness.requests.length;
          const fromLog = harness.devLog.length;
          server.resetHandlers();
          server.use(harness.responds(() => HttpResponse.json(delivery as never)));
          const view = render(
            createElement(
              'main',
              null,
              createElement(LiveViewLauncher, { zone_id: ZONE_ID, clock: harness.clock }),
            ),
          );
          try {
            clickRequest();
            const link = await screen.findByRole('link', { name: text('live_view.link') });
            // La pestaña nueva no tiene `opener` ni envía referente (BR-APP-65).
            expect(link.getAttribute('rel')).toBe('noopener noreferrer');
            expect(link.getAttribute('target')).toBe('_blank');
            expect(open).not.toHaveBeenCalled();

            if (opening === 'click') {
              fireEvent.click(link);
            } else if (opening === 'middle') {
              // El botón central abre la pestaña sin `click`: `auxclick` (botón 1) cierra igual.
              auxClick(link, 1);
            } else {
              act(() => {
                harness.clock.advance(LIVE_VIEW_TOKEN_SECONDS * 1000);
              });
            }

            expect(document.querySelector('[data-part="link"]')).toBeNull();
            expect(tokenFoundIn(token)).toEqual([]);
            expect(
              JSON.stringify(harness.devLog.slice(fromLog).map((entry) => ({ ...entry }))),
            ).not.toContain(token);
            expect(JSON.stringify(consoles.flatMap((spy) => spy.mock.calls))).not.toContain(token);
            for (const request of harness.requests.slice(fromRequests)) {
              expect(request.url).not.toContain(token);
              expect(JSON.stringify(request.init ?? {})).not.toContain(token);
            }
          } finally {
            view.unmount();
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'el acuse de apertura no lleva el token',
    async () => {
      const harness = liveViewHarness();
      await fc.assert(
        fc.asyncProperty(liveViewDelivery(), async (delivery) => {
          const token = delivery.token as string;
          const opened: unknown[] = [];
          server.resetHandlers();
          server.use(harness.responds(() => HttpResponse.json(delivery as never)));
          const view = render(
            createElement(
              'main',
              null,
              createElement(LiveViewLauncher, {
                zone_id: ZONE_ID,
                clock: harness.clock,
                onOpened: (event: unknown) => {
                  opened.push(event);
                },
              }),
            ),
          );
          try {
            clickRequest();
            const link = await screen.findByRole('link', { name: text('live_view.link') });
            fireEvent.click(link);
            expect(opened).toHaveLength(1);
            expect(JSON.stringify(opened)).not.toContain(token);
          } finally {
            view.unmount();
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
