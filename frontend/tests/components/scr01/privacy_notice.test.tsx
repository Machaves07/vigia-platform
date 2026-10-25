/**
 * UI-08 `PrivacyNoticeGate` (IF-03; BR-APP-13; NFR-APP-33) con el transporte real y MSW:
 *
 * - Una respuesta `privacy_notice_required` en cualquier pantalla pasa la sesión a
 *   `privacy_notice_pending` y la puerta ocupa la pantalla: solo dos acciones alcanzables
 *   («Acepto» y «Cerrar sesión»), en esa ruta y en cualquier otra por URL.
 * - «Acepto» envía la versión mostrada, renueva el `Principal` y vuelve a la ruta guardada.
 * - Al entrar por primera vez (sin `Principal`), la versión es la única publicada y el texto es
 *   el marcador de `backend/resources/privacy-notice`.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { server } from '../../msw/server';
import {
  currentMe,
  expectNoSerious,
  location,
  mount,
  ORIGIN,
  seriousViolations,
  WAIT,
  type Fixture,
} from './support';

const VERSION = 'v0-pendiente';
// `npm test` corre en `frontend/`: el texto que sirve U-02, leído del backend.
const NOTICE = readFileSync(
  join(process.cwd(), '..', 'backend', 'resources', 'privacy-notice', `${VERSION}.md`),
  'utf8',
);

function required() {
  return HttpResponse.json(
    {
      code: 'privacy_notice_required',
      detail_code: null,
      message_es: 'Acepte el aviso',
      correlation_id: '0190a1b2-0000-7000-8000-0000000000f2',
      retry_after_seconds: null,
    },
    { status: 403 },
  );
}

/** Elementos que se pueden activar con el teclado o el puntero dentro de `root`. */
function reachable(root: HTMLElement): string[] {
  return [
    ...root.querySelectorAll<HTMLElement>(
      'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable="true"], [role="button"], [role="link"]',
    ),
  ].map((element) => `${element.tagName.toLowerCase()}:${element.textContent}`);
}

/** Sesión activa en `/zones/zona-sintetica?periodo=7d` que recibe `privacy_notice_required`. */
async function pendingAt(path = '/zones/zona-sintetica?periodo=7d'): Promise<Fixture> {
  const me = currentMe();
  const principal = { ...me, privacy_notice_version: VERSION };
  let accepted = false;
  server.use(
    http.get(`${ORIGIN}/me`, () => (accepted ? HttpResponse.json(principal) : required())),
    http.post(`${ORIGIN}/privacy-notice/accept`, async ({ request }) => {
      const body = (await request.json()) as { notice_version?: string };
      if (body.notice_version !== VERSION) {
        return HttpResponse.json(
          {
            code: 'invalid_request',
            detail_code: null,
            message_es: 'Versión distinta',
            correlation_id: '0190a1b2-0000-7000-8000-0000000000f3',
            retry_after_seconds: null,
          },
          { status: 400 },
        );
      }
      accepted = true;
      return HttpResponse.json({ notice_version: VERSION, newly_accepted: true });
    }),
  );
  accepted = true;
  const fixture = mount(path);
  await fixture.session.load();
  accepted = false;
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Consultar' }, WAIT));
  await screen.findByRole('heading', { level: 1, name: text('scr01.privacy.title') }, WAIT);
  return fixture;
}

describe('aviso de tratamiento (UI-08)', { timeout: 60_000 }, () => {
  it('solo dos acciones alcanzables; tras aceptar vuelve a la ruta guardada', async () => {
    const accepts: unknown[] = [];
    server.events.on('request:start', ({ request }) => {
      if (request.url.endsWith('/privacy-notice/accept')) accepts.push(request.method);
    });
    try {
      const fixture = await pendingAt();
      expect(fixture.session.getSnapshot().status).toBe('privacy_notice_pending');
      expect(fixture.session.getSnapshot().return_route).toBe('/zones/zona-sintetica?periodo=7d');
      expect(reachable(document.body)).toEqual([
        `button:${text('scr01.privacy.accept')}`,
        `button:${text('scr01.privacy.logout')}`,
      ]);
      expect(screen.queryByRole('heading', { name: 'Pantalla sintética' })).toBeNull();
      expect(screen.getByText(VERSION).tagName).toBe('STRONG');

      // Por URL, otra ruta tampoco muestra nada más.
      await fixture.router.navigate('/home');
      await screen.findByRole('heading', { level: 1, name: text('scr01.privacy.title') }, WAIT);
      expect(reachable(document.body)).toHaveLength(2);
      await fixture.router.navigate('/zones/zona-sintetica?periodo=7d');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: text('scr01.privacy.accept') }));
      await waitFor(() => {
        expect(fixture.session.getSnapshot().status).toBe('active');
      }, WAIT);
      expect(location(fixture)).toBe('/zones/zona-sintetica?periodo=7d');
      expect(
        await screen.findByRole('heading', { level: 1, name: 'Pantalla sintética' }, WAIT),
      ).toBeDefined();
      expect(fixture.session.getSnapshot().return_route).toBeNull();
      expect(accepts).toEqual(['POST']);
    } finally {
      server.events.removeAllListeners('request:start');
    }
  });

  it('al entrar por primera vez la versión es la publicada y el texto el marcador de U-02', async () => {
    const me = currentMe();
    let accepted = false;
    const versions: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/auth/login`, () => HttpResponse.json({ status: 'authenticated' })),
      http.get(`${ORIGIN}/me`, () =>
        accepted ? HttpResponse.json({ ...me, privacy_notice_version: VERSION }) : required(),
      ),
      http.post(`${ORIGIN}/privacy-notice/accept`, async ({ request }) => {
        versions.push(await request.json());
        accepted = true;
        return HttpResponse.json({ notice_version: VERSION, newly_accepted: true });
      }),
    );
    const fixture = mount('/login');
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText(text('scr01.login.email'), {}, WAIT),
      'persona@ejemplo.test',
    );
    await user.type(screen.getByLabelText(text('scr01.login.password')), 'clave-sintetica-1');
    await user.click(screen.getByRole('button', { name: text('scr01.login.submit') }));
    await screen.findByRole('heading', { level: 1, name: text('scr01.privacy.title') }, WAIT);
    expect(fixture.session.getSnapshot().principal).toBeNull();
    expect(screen.getByText(VERSION).tagName).toBe('STRONG');
    const title = NOTICE.split('\n')[0]?.replace(/^#\s+/, '');
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(title);
    await user.click(screen.getByRole('button', { name: text('scr01.privacy.accept') }));
    await waitFor(() => {
      expect(location(fixture)).toBe('/home');
    }, WAIT);
    expect(versions).toEqual([{ notice_version: VERSION }]);
  });

  it('una versión que el servidor no acepta deja la puerta con su aviso', async () => {
    const fixture = await pendingAt();
    server.use(
      http.post(`${ORIGIN}/privacy-notice/accept`, () =>
        HttpResponse.json(
          {
            code: 'invalid_request',
            detail_code: null,
            message_es: 'Versión distinta',
            correlation_id: '0190a1b2-0000-7000-8000-0000000000f4',
            retry_after_seconds: null,
          },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: text('scr01.privacy.accept') }));
    expect((await screen.findByRole('alert', {}, WAIT)).getAttribute('data-text-key')).toBe(
      'scr01.privacy.rejected',
    );
    expect(fixture.session.getSnapshot().status).toBe('privacy_notice_pending');
  });

  it('«Cerrar sesión» cierra en el servidor y vuelve a /login', async () => {
    let logouts = 0;
    server.use(
      http.post(`${ORIGIN}/auth/logout`, () => {
        logouts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const fixture = await pendingAt();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: text('scr01.privacy.logout') }));
    await waitFor(() => {
      expect(location(fixture)).toBe('/login');
    }, WAIT);
    expect(logouts).toBe(1);
    expect(fixture.session.getSnapshot().status).toBe('anonymous');
  });

  it('axe sin violaciones serias', async () => {
    const fixture = await pendingAt();
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
