/** `AssigneePicker` (TASK-583): solo pide la lista con `actions.manage` y un hallazgo. */
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AssigneePicker } from '@/pages/scr09/loop/AssigneePicker';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import {
  assigneesHandler,
  FINDING,
  INSTANT,
  SESSION_ASSIGNEE,
  USER,
  WAIT,
} from './classify_support';

const OTHER = {
  user_id: '0190a1b2-4444-7000-8000-000000000002',
  display_name: 'Elena Prueba',
  roles: ['line_manager' as const],
};

async function rendered(permissions: string[], findingId = FINDING) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => true,
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  const listed = assigneesHandler([SESSION_ASSIGNEE, OTHER]);
  server.use(listed.handler);
  render(
    <ScreenQueryProvider cache={cache}>
      <AssigneePicker
        context={{ findingId, scope: 'org:sintetico', concession: null, permissions }}
        sessionUser={{ id: USER, name: 'Persona sintética' }}
        caption="Responsable"
        value={USER}
        onChange={() => undefined}
      />
    </ScreenQueryProvider>,
  );
  await act(async () => {
    await new Promise((done) => {
      setTimeout(done, 50);
    });
  });
  const values = () =>
    [...screen.getByRole<HTMLSelectElement>('combobox', { name: 'Responsable' }).options].map(
      (option) => option.value,
    );
  return { requests: listed.requests, values, cache };
}

describe('AssigneePicker: quién pide la lista (TASK-583)', { timeout: 40_000 }, () => {
  it('con actions.manage pide la lista y la ofrece', async () => {
    const { requests, values, cache } = await rendered(['actions.manage']);
    await screen.findByRole('option', { name: /Elena Prueba/ }, WAIT);
    expect(requests).toHaveLength(1);
    expect(values()).toEqual([USER, OTHER.user_id]);
    cache.dispose();
  });

  it('sin actions.manage no pide nada y solo ofrece a quien está en sesión', async () => {
    const { requests, values, cache } = await rendered(['findings.read']);
    expect(requests).toEqual([]);
    expect(values()).toEqual([USER]);
    cache.dispose();
  });

  it('sin hallazgo no pide nada', async () => {
    const { requests, cache } = await rendered(['actions.manage'], '');
    expect(requests).toEqual([]);
    cache.dispose();
  });
});
