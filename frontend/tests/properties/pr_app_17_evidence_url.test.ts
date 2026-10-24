/**
 * PR-APP-17 (BR-APP-58): para toda secuencia de abrir, reproducir (botón propio o control nativo,
 * también dos pulsaciones seguidas), pausar, esperar y reanudar, el reproductor pide exactamente
 * una URL por clip y apertura, y una nueva solo tras vencer (el menor entre `expires_at` y 5
 * minutos, con el reloj de la prueba); ninguna URL queda en la caché de consultas o mutaciones, en
 * `localStorage`/`sessionStorage` (preferencias), en el texto visible ni, tras cerrar, en el DOM.
 *
 * El oráculo es un modelo independiente del componente: por apertura, el instante en que vence
 * la URL vigente. Las URL son únicas por petición, así que comparar el `src` con la última
 * entregada es exacto.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import fc from 'fast-check';
import { createElement } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FakeClock } from '../support/fakeClock';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';
import { BlurredClipPlayer } from '@/components/BlurredClipPlayer/BlurredClipPlayer';
import type { ReadUrl } from '@/ports/upload';
import { EVIDENCE_URL_MAX_SECONDS } from '@/state/reducers/countdown';

const START = Date.UTC(2026, 9, 24, 12);
const CLIPS = ['0192d3e4-0000-7000-8000-000000000001', '0192d3e4-0000-7000-8000-000000000002'];

type Command =
  | { readonly type: 'play'; readonly via: 'button' | 'native' }
  | { readonly type: 'double_play' }
  | { readonly type: 'pause' }
  | { readonly type: 'wait'; readonly ms: number }
  | { readonly type: 'reopen'; readonly clip: number };

const waitMs = fc.oneof(
  fc.integer({ min: 0, max: 400_000 }),
  // Bordes del vencimiento: justo antes, justo en y justo después de 5 minutos.
  fc.constantFrom(1, 299_999, 300_000, 300_001),
);

const command: fc.Arbitrary<Command> = fc.oneof(
  fc.record({
    type: fc.constant('play' as const),
    via: fc.constantFrom<'button' | 'native'>('button', 'native'),
  }),
  fc.constant({ type: 'double_play' as const }),
  fc.constant({ type: 'pause' as const }),
  fc.record({ type: fc.constant('wait' as const), ms: waitMs }),
  fc.record({ type: fc.constant('reopen' as const), clip: fc.integer({ min: 0, max: 1 }) }),
);

/** Vigencias que el servidor declara, en segundos: dentro y fuera del tope de 5 minutos. */
const ttls = fc.array(fc.oneof(fc.integer({ min: 1, max: 900 }), fc.constantFrom(299, 300, 301)), {
  minLength: 1,
  maxLength: 8,
});

function storageDump(storage: Storage): string {
  const entries: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null) entries.push(key, storage.getItem(key) ?? '');
  }
  return entries.join('\n');
}

function cacheDump(client: QueryClient): string {
  return JSON.stringify([
    client
      .getQueryCache()
      .getAll()
      .map((query) => [query.queryKey, query.state.data]),
    client
      .getMutationCache()
      .getAll()
      .map((mutation) => [mutation.state.data, mutation.state.variables]),
  ]);
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 4; round += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockReturnValue(undefined);
  localStorage.clear();
  sessionStorage.clear();
});

describe(
  'PR-APP-17: una URL por clip y apertura, otra solo tras vencer',
  { timeout: PROPERTY_TIMEOUT_MS },
  () => {
    it('se cumple para toda secuencia de abrir, reproducir, esperar y reanudar', async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.array(command, { minLength: 1, maxLength: 16 }),
          ttls,
          async (commands, serverTtls) => {
            const clock = new FakeClock(START);
            const client = new QueryClient();
            /** Peticiones reales: clip pedido y URL entregada. */
            const requests: { clip: string; url: string }[] = [];
            const requestUrl = (clip: string): Promise<ReadUrl> => {
              const ttl = serverTtls[requests.length % serverTtls.length] ?? 300;
              const url = `https://store.example/clip/${clip}?sig=${String(requests.length).padStart(4, '0')}`;
              requests.push({ clip, url });
              return Promise.resolve({
                url,
                expires_at: new Date(clock.now() + ttl * 1000).toISOString(),
              });
            };

            let clip = CLIPS[0] ?? '';
            /** Modelo: vencimiento de la URL de la apertura en curso (`null`: aún sin URL). */
            let expiresAt: number | null = null;
            let expected = 0;
            let hasUrl = false;

            const view = render(
              createElement(
                QueryClientProvider,
                { client },
                createElement(BlurredClipPlayer, { key: 0, evidenceId: clip, clock, requestUrl }),
              ),
            );
            await settle();
            // Al abrir no se pide nada.
            expect(requests).toHaveLength(0);

            const video = (): HTMLVideoElement => {
              const element = view.container.querySelector('video');
              if (element === null) throw new Error('sin <video>');
              return element;
            };
            /** El modelo: una pulsación pide si no hay URL vigente. */
            const press = () => {
              if (expiresAt === null || clock.now() >= expiresAt) {
                const ttl = serverTtls[expected % serverTtls.length] ?? 300;
                expected += 1;
                expiresAt = clock.now() + Math.min(ttl, EVIDENCE_URL_MAX_SECONDS) * 1000;
                return true;
              }
              return false;
            };

            let opening = 0;
            for (const step of commands) {
              switch (step.type) {
                case 'play': {
                  const button = view.container.querySelector('button');
                  if (step.via === 'button' && button !== null) fireEvent.click(button);
                  else fireEvent.play(video());
                  if (press()) hasUrl = true;
                  break;
                }
                case 'double_play':
                  fireEvent.play(video());
                  fireEvent.play(video());
                  if (press()) hasUrl = true;
                  break;
                case 'pause':
                  fireEvent.pause(video());
                  break;
                case 'wait':
                  act(() => {
                    clock.advance(step.ms);
                  });
                  break;
                case 'reopen': {
                  const previous = hasUrl ? (requests.at(-1)?.url ?? null) : null;
                  opening += 1;
                  clip = CLIPS[step.clip] ?? clip;
                  view.rerender(
                    createElement(
                      QueryClientProvider,
                      { client },
                      createElement(BlurredClipPlayer, {
                        key: opening,
                        evidenceId: clip,
                        clock,
                        requestUrl,
                      }),
                    ),
                  );
                  expiresAt = null;
                  hasUrl = false;
                  await settle();
                  if (previous !== null) {
                    expect(document.documentElement.outerHTML.includes(previous)).toBe(false);
                  }
                  break;
                }
              }
              await settle();

              expect(requests).toHaveLength(expected);
              if (hasUrl) {
                expect(requests.at(-1)?.clip).toBe(clip);
                expect(video().getAttribute('src')).toBe(requests.at(-1)?.url);
              } else {
                expect(video().hasAttribute('src')).toBe(false);
              }
              const places = {
                cache: cacheDump(client),
                localStorage: storageDump(localStorage),
                sessionStorage: storageDump(sessionStorage),
                text: document.body.textContent,
                title: document.title,
                location: window.location.href,
              };
              for (const request of requests) {
                expect(
                  Object.entries(places)
                    .filter(([, content]) => content.includes(request.url))
                    .map(([place]) => place),
                ).toEqual([]);
              }
            }
            cleanup();
            client.clear();
          },
        ),
      );
    });
  },
);
