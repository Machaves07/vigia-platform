import { act, screen, within, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, expect, it } from 'vitest';
import { SCREENS } from '@/app/screens';
import { text } from '@/texts';
import { concessionsPort } from '@/ports/scr02Concessions';
import { mountAt, providerConcession } from '../components/scr02/support';
import { server } from '../msw/server';
import { mountShell, ORIGIN, serveSession, shellMe, WAIT } from '../support/appShell';

const requests: Request[] = [];
const capture = ({ request }: { request: Request }) => requests.push(request);
afterEach(() => {
  server.events.removeListener('request:start', capture);
  requests.length = 0;
});

it('A-8 · BR-APP-68, 69: al vencer, la petición siguiente ya no lleva concesión', async () => {
  const env = await mountAt('/provider/concessions');
  await act(async () => {
    env.concession.activate(
      providerConcession({ expires_at: new Date(env.clock.now() + 1_000).toISOString() }),
    );
    await concessionsPort.hierarchy.read();
  });
  expect(env.hierarchyHeaders.at(-1)).toBe(providerConcession().concession_id);
  await act(async () => {
    env.clock.advance(1_000);
    await concessionsPort.hierarchy.read();
  });
  expect(env.concession.header()).toBeNull();
  expect(env.hierarchyHeaders.at(-1)).toBeNull();
}, 30_000);

it('A-8 · BR-APP-67 a 69; A-46, A-48, A-69: proveedor sin concesión solo ve su panel y nunca envía cabecera', async () => {
  serveSession(shellMe(['concessions.grant'], { kind: 'provider', role: 'provider_installer' }));
  server.use(
    http.get(`${ORIGIN}/provider/concessions`, () => HttpResponse.json({ concessions: [] })),
  );
  server.events.on('request:start', capture);
  const env = mountShell('/provider/concessions', SCREENS);
  await screen.findByRole('button', { name: text('scr02.request.open') }, WAIT);
  await waitFor(() => {
    expect(env.services.cache.client.isFetching()).toBe(0);
  }, WAIT);
  const nav = screen.getByRole('navigation', { name: text('app_shell.nav.label') });
  expect(
    within(nav)
      .getAllByRole('link')
      .map((link) => link.getAttribute('href')),
  ).toEqual(['/provider/concessions']);
  expect(env.services.concession.header()).toBeNull();
  expect(requests.length).toBeGreaterThanOrEqual(3);
  expect(requests.map((request) => request.headers.get('X-Vigia-Concession'))).toEqual(
    requests.map(() => null),
  );
  expect(screen.queryByRole('button', { name: /clasificar|cerrar/i })).toBeNull();
}, 30_000);
