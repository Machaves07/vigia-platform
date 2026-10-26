/** IF-22 / D-4 sobre transporte, caché y puerta de actividad reales. */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { HttpResponse, http } from 'msw';
import { descriptor } from '@/pages/scr15/descriptor';
import { readNotificationPages } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { settle } from '../../support/queryCache';
import { mountShell, serveSession, shellMe, START, syntheticScreen } from '../../support/appShell';
import { server } from '../../msw/server';
import { CURSOR, WAIT, apiError, notification, setup } from './support';
import { USER_B } from '../../support/session';

describe('SCR-15 campana', { timeout: 60_000 }, () => {
  it('la cabecera real monta la campana y permite recorrer lista y preferencias', async () => {
    serveSession(shellMe(['notifications.read']));
    server.use(
      http.get('http://localhost/notifications', () =>
        HttpResponse.json({ items: [notification()], next_after: null }),
      ),
      http.get('http://localhost/notifications/preferences', () =>
        HttpResponse.json({
          email_enabled_normal: false,
          email_enabled_digest: true,
          weekly_digest_weekday: 1,
          updated_at: '2026-01-01T12:00:00.000Z',
          email_suppressed_hard_bounce: false,
          email_suppressed_complaint: false,
        }),
      ),
    );
    const fixture = mountShell('/notifications', [descriptor]);
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    fireEvent.click(
      within(screen.getByRole('banner')).getByRole('link', { name: text('navigation.bell') }),
    );
    await screen.findByRole('button', { name: text('scr15.mark_read') }, WAIT);
    // El menú también contiene la ruta; se elige el enlace específico del contenido.
    fireEvent.click(screen.getByRole('link', { name: text('scr15.preferences') }));
    await screen.findByRole('form', { name: text('scr15.preferences') }, WAIT);
    expect(fixture.route()).toBe('/notifications/preferences');
  });
  it('cuenta todas las páginas sin presentar la primera página como total', async () => {
    const { calls, view } = await setup({ page: 'bell', unreadMore: true });
    await waitFor(() => {
      expect(screen.queryByText(text('scr15.count_unavailable'))).toBeNull();
    }, WAIT);
    expect(screen.getByRole('status').textContent).toBe(`${text('scr15.unread')}: 2`);
    expect(calls.reads.map((url) => Object.fromEntries(url.searchParams))).toEqual([
      { limit: '200', status: 'unread' },
      { limit: '200', status: 'unread', after: CURSOR },
    ]);
    expect(screen.getByRole('link', { name: text('navigation.bell') }).getAttribute('href')).toBe(
      '/notifications',
    );
    expect(
      (await axe(view.container)).violations.filter(
        (v) => v.impact === 'serious' || v.impact === 'critical',
      ),
    ).toEqual([]);
  });

  it('sondea a los 60 s, se detiene exactamente a los 25 min y reanuda tras interacción', async () => {
    const { calls, clock, activity } = await setup({ page: 'bell' });
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    await act(async () => {
      clock.advance(59_999);
      await settle();
    });
    expect(calls.reads).toHaveLength(1);
    await act(async () => {
      clock.advance(1);
      await settle();
    });
    await waitFor(() => {
      expect(calls.reads).toHaveLength(2);
    }, WAIT);
    for (let minute = 2; minute < 25; minute++) {
      await act(async () => {
        clock.advance(60_000);
        await settle();
      });
      await waitFor(() => {
        expect(calls.reads).toHaveLength(minute + 1);
      }, WAIT);
    }
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(activity.isOpen()).toBe(false);
    expect(calls.reads).toHaveLength(25);
    await act(async () => {
      clock.advance(120_000);
      await settle();
    });
    expect(calls.reads).toHaveLength(25);
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(activity.isOpen()).toBe(true);
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(calls.reads).toHaveLength(26);
    }, WAIT);
  });

  it('respeta pestaña oculta y circuito cerrado sin mentir con un cero ante fallos', async () => {
    const { calls, gates, clock } = await setup({ page: 'bell' });
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    gates.visible = false;
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(calls.reads).toHaveLength(1);
    gates.visible = true;
    gates.platform = false;
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(calls.reads).toHaveLength(1);
    gates.platform = true;
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(calls.reads).toHaveLength(2);
    }, WAIT);
  });

  it('un error de lectura no se presenta como cero no leídas', async () => {
    const { cache } = await setup({ page: 'bell', listResponse: () => apiError() });
    await waitFor(() => {
      expect(cache.client.getQueryCache().getAll()[0]?.state.status).toBe('error');
    }, WAIT);
    expect(screen.getByText(text('scr15.count_unavailable')).textContent).toBe(
      text('scr15.count_unavailable'),
    );
    expect(screen.queryByText(`${text('scr15.unread')}: 0`)).toBeNull();
  });

  it('si falla un refresco, deja de presentar el recuento anterior como vigente', async () => {
    const { cache } = await setup({ page: 'bell' });
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    server.use(http.get('http://localhost/notifications', () => apiError()));
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await waitFor(() => {
      expect(cache.client.getQueryCache().getAll()[0]?.state.status).toBe('error');
    }, WAIT);
    await screen.findByText(text('scr15.count_unavailable'), {}, WAIT);
    expect(screen.queryByText(`${text('scr15.unread')}: 1`)).toBeNull();
  });

  it('un cursor repetido falla cerrado en vez de mantener un bucle de sondeo', async () => {
    const { calls } = await setup({
      page: 'bell',
      permissions: [],
      listResponse: () => HttpResponse.json({ items: [notification()], next_after: CURSOR }),
    });
    await expect(readNotificationPages(3, new AbortController().signal, true)).rejects.toThrow(
      'Cursor de notificaciones repetido.',
    );
    expect(calls.reads).toHaveLength(2);
  });

  it('la cabecera no pide datos propios sin el permiso declarado', async () => {
    const { calls, clock } = await setup({ page: 'bell', permissions: [] });
    await act(async () => {
      await settle();
    });
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(calls.reads).toHaveLength(0);
  });

  it('cada cambio de persona, organización o alcance usa otra clave propia', async () => {
    const { calls, me, session } = await setup({ page: 'bell' });
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    const changes = [
      () => {
        me.user.user_id = USER_B;
      },
      () => {
        me.organization.organization_id = '0190a1b2-0000-7000-8000-0000000000f2';
      },
      () => {
        const assignment = me.assignments[0];
        if (assignment !== undefined) assignment.scope_id = '0190a1b2-0000-7000-8000-0000000000f3';
      },
    ];
    for (let index = 0; index < changes.length; index++) {
      await act(async () => {
        changes[index]?.();
        await session.load();
        await settle();
      });
      await waitFor(() => {
        expect(calls.reads).toHaveLength(index + 2);
      }, WAIT);
    }
  });

  it('A-69: no consulta notificaciones propias bajo concesión ni la termina por un not_found', async () => {
    const { calls, me, session, clock } = await setup({ page: 'bell' });
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    await act(async () => {
      me.concession_id = '0190a1b2-0000-7000-8000-0000000000c1';
      await session.load();
      await settle();
      clock.advance(60_000);
      await settle();
    });
    await screen.findByText(text('scr15.own_without_concession'), {}, WAIT);
    expect(calls.reads).toHaveLength(1);
    expect(screen.queryByRole('link', { name: text('navigation.bell') })).toBeNull();
    await act(async () => {
      me.concession_id = null;
      await session.load();
      await settle();
    });
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(calls.reads).toHaveLength(2);
    }, WAIT);
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
  });

  it('suspende la campana desde la activación, antes de que GET /me refleje la concesión', async () => {
    let reads = 0;
    serveSession(shellMe(['fleet.read', 'notifications.read'], { kind: 'provider' }));
    server.use(
      http.get('http://localhost/notifications', () => {
        reads++;
        return HttpResponse.json({ items: [notification()], next_after: null });
      }),
    );
    const fixture = mountShell('/fleet', [
      syntheticScreen({
        id: 'SCR-07',
        title: 'navigation.scr07',
        path: '/fleet',
        permission: 'fleet.read',
        page: () => null,
      }),
    ]);
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    await act(async () => {
      expect(
        fixture.services.concession.activate({
          concession_id: '0190a1b2-0000-7000-8000-0000000000d1',
          client_organization_id: '0190a1b2-0000-7000-8000-0000000000f2',
          scope_level: 'organization',
          scope_id: '0190a1b2-0000-7000-8000-0000000000f2',
          expires_at: new Date(START + 3_600_000).toISOString(),
          status: 'active',
        }),
      ).toBe(true);
      await settle();
    });
    await screen.findByText(text('scr15.own_without_concession'), {}, WAIT);
    await act(async () => {
      fixture.clock.advance(60_000);
      await settle();
    });
    expect(reads).toBe(1);
    expect(fixture.services.concession.header()).not.toBeNull();
    expect(fixture.services.session.getSnapshot().principal?.concession_id).toBeNull();
  });
});
