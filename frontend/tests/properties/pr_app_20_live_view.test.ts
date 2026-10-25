/**
 * PR-APP-20 (BR-APP-65, PAT-APP-SEG-03, D-1): para toda entrega de vista en vivo generada, la URL
 * abierta lleva el token **solo en el fragmento** —nunca en la ruta, la consulta, las credenciales
 * ni una cabecera—, y la petición que lo trae no lo devuelve al servidor.
 *
 * Las propiedades comparan contra el token y la dirección **generados**, nunca contra textos fijos
 * (VIG-194). La última comprueba la URL que la interfaz pone de verdad en el enlace, no solo la
 * composición pura.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse } from 'msw';
import { createElement } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { installTransport } from '@/ports/base';
import { liveViewLink } from '@/ports/liveview';
import { text } from '@/texts';

import { liveViewDelivery, liveViewToken, localUrl } from '../generators';
import { server } from '../setup/msw';
import { liveViewHarness, ZONE_ID } from '../support/liveView';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

afterEach(() => {
  installTransport(null);
});

/** Invariantes de PR-APP-20: la URL es el origen del nodo, la ruta `/` y el token en el fragmento. */
function expectLinkOnlyInFragment(href: string, token: string): void {
  const url = new URL(href);
  expect(url.protocol).toBe('https:');
  expect(url.search).toBe('');
  expect(url.username).toBe('');
  expect(url.password).toBe('');
  expect(url.pathname).toBe('/');
  expect(url.hash).toBe(`#token=${token}`);
  expect(href).toBe(`${url.origin}/#token=${token}`);
}

/**
 * Además, fuera del fragmento no queda rastro del token. Solo se usa con los tokens del generador
 * (32 a 64 caracteres de `base64url`), que no pueden aparecer por casualidad en el origen.
 */
function expectNoTraceOutsideFragment(href: string, token: string): void {
  const url = new URL(href);
  expect(href.slice(0, href.length - url.hash.length)).not.toContain(token);
  expect(href.split(token)).toHaveLength(2);
}

function linkOf(delivery: Record<string, unknown>) {
  return liveViewLink({
    token: delivery.token,
    live_view_local_url: delivery.live_view_local_url,
  });
}

function clickRequest(): void {
  const button = document.querySelector<HTMLButtonElement>('[data-part="request"]');
  if (button === null) throw new Error('Falta el botón de abrir la vista en vivo.');
  fireEvent.click(button);
}

/** Direcciones locales hostiles o incompletas: la composición debe cerrarse o normalizarlas. */
const hostileLocal = fc.oneof(
  fc.constant(null),
  fc.constant(undefined),
  fc.constant(''),
  fc.constant('   '),
  fc.string(),
  fc.webUrl(),
  fc.constant('https://usuario:clave@nodo.local:8443/?x=1#token=otro'),
  fc.constant('http://nodo.local:8443/'),
  fc.constant('data:text/html,<b>hola</b>'),
  fc.constant('ftp://nodo.local:8443/'),
  fc.constant('nodo.local:8443'),
  fc.constant(`https://nodo.local:8443/${'a'.repeat(300)}`),
  fc.integer(),
  fc.boolean(),
);

describe('PR-APP-20 · el token solo en el fragmento', () => {
  it(
    'toda entrega generada produce una URL con el token solo en el fragmento',
    () => {
      fc.assert(
        fc.property(liveViewDelivery(), (delivery) => {
          const token = delivery.token as string;
          const link = linkOf(delivery);
          expect(link.kind).toBe('link');
          if (link.kind !== 'link') return;
          expectLinkOnlyInFragment(link.href, token);
          expectNoTraceOutsideFragment(link.href, token);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'con una dirección hostil: o no hay enlace, o el token va solo en el fragmento',
    () => {
      fc.assert(
        fc.property(hostileLocal, liveViewToken(), (local, token) => {
          const link = liveViewLink({ token, live_view_local_url: local });
          if (link.kind === 'no_local_url') {
            expect(local === null || local === undefined || local === '').toBe(true);
            return;
          }
          if (link.kind === 'unusable') return;
          expectLinkOnlyInFragment(link.href, token);
          expectNoTraceOutsideFragment(link.href, token);
          // Nada del valor recibido se arrastra: ni credenciales, ni consulta, ni su fragmento.
          expect(link.href).not.toContain('usuario');
          expect(link.href).not.toContain('clave');
          expect(link.href).not.toContain('?');
          expect(link.href).not.toContain('#token=otro');
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'un token que no tiene la forma del contrato no llega al enlace',
    () => {
      const malformed = fc.string({
        minLength: 1,
        maxLength: 64,
        unit: fc.constantFrom('#', '?', '&', '/', '\\', ' ', '%', '"', ':', '@', '=', '+', '\n'),
      });
      fc.assert(
        fc.property(localUrl(), malformed, (local, token) => {
          expect(liveViewLink({ token, live_view_local_url: local })).toEqual({ kind: 'unusable' });
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'un token de la forma del contrato tampoco se escapa del fragmento',
    () => {
      const shaped = fc.stringMatching(/^[A-Za-z0-9_-]{1,64}$/);
      fc.assert(
        fc.property(localUrl(), shaped, (local, token) => {
          const link = liveViewLink({ token, live_view_local_url: local });
          expect(link.kind).toBe('link');
          if (link.kind !== 'link') return;
          expectLinkOnlyInFragment(link.href, token);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'la URL que la interfaz pone en el enlace lleva el token solo en el fragmento',
    async () => {
      const harness = liveViewHarness();
      await fc.assert(
        fc.asyncProperty(liveViewDelivery(), async (delivery) => {
          const token = delivery.token as string;
          const from = harness.requests.length;
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
            const href = link.getAttribute('href') ?? '';
            expectLinkOnlyInFragment(href, token);
            expectNoTraceOutsideFragment(href, token);
            // La petición que trajo el token no lo devuelve: ni consulta, ni cuerpo, ni cabecera.
            const sent = harness.requests.slice(from);
            expect(sent).toHaveLength(1);
            for (const request of sent) {
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
});
