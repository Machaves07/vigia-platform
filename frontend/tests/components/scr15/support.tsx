/** Servicios reales y respuestas sintéticas de SCR-15; ninguna llamada a una API externa. */
import { cleanup, render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { Outlet, RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach } from 'vitest';
import { Bell } from '@/pages/scr15/Bell';
import List from '@/pages/scr15/list';
import PreferencesPage from '@/pages/scr15/preferences';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { Notification, Preferences } from '@/ports/scr15Notifications';
import { ScreenQueryProvider, createScreenQueryCache } from '@/state/query';
import { SessionProvider, createActivityGate, createSessionStore } from '@/state/session';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';

export const WAIT = { timeout: 10_000 };
export const ID = '0190a1b2-0000-7000-8000-000000000001';
export const SECOND = '0190a1b2-0000-7000-8000-000000000002';
export const INSTANT = '2026-03-01T12:00:00.000Z';
export const CURSOR = 'c3ludGhldGljLWN1cnNvcg';
export function notification(id = ID): Notification {
  return {
    notification_id: id,
    event_name: 'finding_received',
    urgency: 'normal',
    title_es: 'Hallazgo sintético recibido',
    body_es: 'Cuerpo sintético no mostrado.',
    deep_link_ref: { kind: 'finding', id },
    created_at: INSTANT,
    read_at: null,
    archived_at: null,
    email_status: null,
  };
}
export function preferences(): Preferences {
  return {
    email_enabled_normal: false,
    email_enabled_digest: true,
    weekly_digest_weekday: 7,
    updated_at: INSTANT,
    email_suppressed_hard_bounce: true,
    email_suppressed_complaint: true,
  };
}
export function apiError(status = 404, code = 'not_found') {
  return HttpResponse.json(
    { code, message_es: 'Error sintético', correlation_id: ID, retry_after_seconds: null },
    { status },
  );
}
const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function setup(
  options: {
    page?: 'bell' | 'list' | 'preferences';
    items?: Notification[];
    more?: Notification[];
    unreadMore?: boolean;
    withBell?: boolean;
    listResponse?: () => Response;
    preferencesResponse?: () => Response;
    markResponse?: () => Response | Promise<Response>;
    saveResponse?: () => Response;
    permissions?: string[];
  } = {},
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const activity = createActivityGate({ clock, target: window });
  const gates = { visible: true, platform: true };
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => gates.visible,
      canReadPlatform: () => gates.platform,
      isActive: () => activity.isOpen(),
    },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 3_600_000,
    absoluteExpiresAt: clock.now() + 43_200_000,
    permissions: options.permissions ?? ['notifications.read'],
  });
  const session = createSessionStore({
    clock,
    cache,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    clearPreferences: () => undefined,
    currentRoute: () => '/notifications',
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(
    createTransport({ clock, connection: createPortConnection(clock), origin: 'http://localhost' }),
  );
  const items = options.items ?? [notification()];
  const more = options.more ?? [];
  let settings = preferences();
  const calls = { reads: [] as URL[], marks: [] as string[], gets: 0, saves: [] as unknown[] };
  server.use(
    http.get('http://localhost/notifications', ({ request }) => {
      const url = new URL(request.url);
      calls.reads.push(url);
      if (options.listResponse !== undefined) return options.listResponse();
      if (url.searchParams.get('status') === 'unread') {
        if (options.unreadMore)
          return HttpResponse.json(
            url.searchParams.has('after')
              ? { items: [notification(SECOND)], next_after: null }
              : { items: [notification()], next_after: CURSOR },
          );
        return HttpResponse.json({
          items: [...items, ...more].filter((item) => item.read_at === null),
          next_after: null,
        });
      }
      return HttpResponse.json(
        url.searchParams.has('after')
          ? { items: more, next_after: null }
          : { items, next_after: more.length === 0 ? null : CURSOR },
      );
    }),
    http.post('http://localhost/notifications/:id/read', ({ params }) => {
      calls.marks.push(String(params.id));
      if (options.markResponse !== undefined) return options.markResponse();
      const item = [...items, ...more].find((item) => item.notification_id === params.id);
      if (item !== undefined) item.read_at = INSTANT;
      return new HttpResponse(null, { status: 204 });
    }),
    http.get('http://localhost/notifications/preferences', () => {
      calls.gets++;
      return options.preferencesResponse?.() ?? HttpResponse.json(settings);
    }),
    http.put('http://localhost/notifications/preferences', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      calls.saves.push(body);
      if (options.saveResponse !== undefined) return options.saveResponse();
      settings = { ...settings, ...body };
      return HttpResponse.json(settings);
    }),
  );
  const page = options.page ?? 'list';
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: (
          <>
            <header>{(options.withBell ?? page === 'bell') && <Bell />}</header>
            <Outlet />
          </>
        ),
        children: [
          { path: 'notifications', element: <List /> },
          { path: 'notifications/preferences', element: <PreferencesPage /> },
          {
            path: 'bell',
            element: (
              <main>
                <h1>{text('scr15.title')}</h1>
              </main>
            ),
          },
          {
            path: 'findings/:id',
            element: (
              <main>
                <h1>{text('scr15.origin')}</h1>
              </main>
            ),
          },
        ],
      },
    ],
    {
      initialEntries: [
        page === 'bell'
          ? '/bell'
          : page === 'preferences'
            ? '/notifications/preferences'
            : '/notifications',
      ],
    },
  );
  const view = render(
    <SessionProvider store={session}>
      <ScreenQueryProvider cache={cache}>
        <RouterProvider router={router} />
      </ScreenQueryProvider>
    </SessionProvider>,
  );
  disposers.push(() => {
    activity.dispose();
    session.dispose();
    cache.dispose();
  });
  return { clock, activity, gates, cache, calls, router, view, session, me };
}
