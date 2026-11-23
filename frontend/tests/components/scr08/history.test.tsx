/**
 * SCR-08, histórico de lo cerrado (TASK-586; O-11; BR-APP-53, 57; UI-14 y UI-20).
 *
 * Con MSW y hallazgos **sintéticos** cerrados y descartados. Las aserciones comparan claves del
 * catálogo, etiquetas de las unidades dueñas, `data-part`, `data-column` y parámetros de la
 * petición por igualdad; nunca subcadenas de texto con datos (VIG-194).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import History from '@/pages/scr08/history';
import Inbox from '@/pages/scr08/inbox';
import type { ClosedFinding, ClosedFindingsPage } from '@/ports/scr08History';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { settle } from '../../support/queryCache';
import { fixture, PLANT, zone } from './support';

const WAIT = { timeout: 10_000 };
const PATH = '/findings/closed';

function closed(index: number, changes: Partial<ClosedFinding> = {}): ClosedFinding {
  return {
    finding_state_id: `0190a1b2-4444-7000-8000-${String(index).padStart(12, '0')}`,
    plant_id: PLANT,
    zone_id: zone().zone_id,
    family: 'coexistence',
    tier: 'tier_1',
    lifecycle_state: 'closed',
    classification_outcome: 'confirmed',
    reason_category: 'guard_bypassed',
    has_action: true,
    has_signed_root_cause: true,
    occurred_at: '2026-01-01T12:00:00.000Z',
    time_aggregated: false,
    aggregation_window_minutes: null,
    ...changes,
  };
}

function discarded(index: number): ClosedFinding {
  return closed(index, {
    lifecycle_state: 'discarded',
    family: 'dwell',
    tier: 'tier_2',
    classification_outcome: 'false_positive',
    reason_category: 'reflection_or_glare',
    has_action: false,
    has_signed_root_cause: false,
    time_aggregated: true,
    aggregation_window_minutes: 60,
  });
}

function body(items: ClosedFinding[], changes: Partial<ClosedFindingsPage> = {}) {
  return { items, partitions: [], next_cursor: null, ...changes };
}

/** El histórico montado con el enrutador real y una ficha de vida de pega para la navegación. */
async function opened(responder: (url: URL) => ClosedFindingsPage) {
  const env = await fixture({ path: PATH, permissions: ['findings.read'] });
  const requests: URL[] = [];
  server.use(
    http.get('http://localhost/loop/closed-findings', ({ request }) => {
      const url = new URL(request.url);
      requests.push(url);
      return HttpResponse.json(responder(url));
    }),
  );
  const view = render(
    <Routes>
      <Route path="/findings/closed" element={<History />} />
      <Route
        path="/findings/:finding_state_id/timeline"
        element={<p data-part="timeline_opened">ficha</p>}
      />
    </Routes>,
    { wrapper: env.Wrapper },
  );
  await waitFor(() => {
    expect(requests.length).toBeGreaterThan(0);
  }, WAIT);
  await act(async () => {
    await settle();
  });
  return { ...env, requests, view };
}

function rows(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('tbody tr')];
}

function cell(row: HTMLElement, column: string): string | null | undefined {
  return row.querySelector(`[data-column="${column}"]`)?.textContent;
}

function params(url: URL): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  for (const key of new Set(url.searchParams.keys())) result[key] = url.searchParams.getAll(key);
  return result;
}

describe('SCR-08 histórico de lo cerrado (TASK-586)', { timeout: 60_000 }, () => {
  it('lista las filas con sus etiquetas: cerrado y descartado por clave del catálogo', async () => {
    await opened(() => body([closed(1), discarded(2)]));
    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    }, WAIT);
    const [first, second] = rows();
    if (first === undefined || second === undefined) throw new Error('faltan filas');
    expect(first.querySelector('[data-part="state"]')?.getAttribute('data-value')).toBe('closed');
    expect(cell(first, 'state')).toBe(text('scr08.inbox.state.closed'));
    expect(cell(second, 'state')).toBe(text('scr08.inbox.state.discarded'));
    expect(cell(first, 'zone')).toBe(zone().name);
    expect(cell(first, 'family')).toBe(text('scr08.inbox.family.coexistence'));
    expect(cell(second, 'family')).toBe(text('scr08.inbox.family.dwell'));
    expect(cell(first, 'tier')).toBe(text('scr08.inbox.tier.tier_1'));
    expect(cell(second, 'tier')).toBe(text('scr08.inbox.tier.tier_2'));
    expect(cell(first, 'reason')).toBe(label('reason_category_confirmed', 'guard_bypassed'));
    expect(cell(second, 'reason')).toBe(
      label('reason_category_false_positive', 'reflection_or_glare'),
    );
    expect([cell(first, 'action'), cell(first, 'root_cause')]).toEqual([
      text('scr08.history.yes'),
      text('scr08.history.yes'),
    ]);
    expect([cell(second, 'action'), cell(second, 'root_cause')]).toEqual([
      text('scr08.history.no'),
      text('scr08.history.no'),
    ]);
    // Zona unipersonal: la hora del servidor con la nota de redondeo; la otra, sin nota.
    expect(second.querySelector('[data-part="aggregated"]')?.textContent).toBe(
      text('scr08.history.aggregated'),
    );
    expect(first.querySelector('[data-part="aggregated"]')).toBeNull();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('scr08.history.title'));
  });

  it('sin categoría, lo dice; el filtro de estado ofrece solo cerrado y descartado', async () => {
    await opened(() => body([closed(1, { classification_outcome: null, reason_category: null })]));
    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    }, WAIT);
    const [row] = rows();
    if (row === undefined) throw new Error('falta la fila');
    expect(cell(row, 'reason')).toBe(text('scr08.history.no_reason'));
    const state = screen.getByLabelText(text('scr08.history.state'));
    expect([...state.querySelectorAll('option')].map((option) => option.value)).toEqual([
      '',
      'closed',
      'discarded',
    ]);
    expect([...state.querySelectorAll('option')].map((option) => option.textContent)).toEqual([
      text('components_structure.keyset.filter_any'),
      text('scr08.inbox.state.closed'),
      text('scr08.inbox.state.discarded'),
    ]);
  });

  it('los filtros se aplican en bloque con el botón, nunca al cambiar un campo', async () => {
    const env = await opened(() => body([closed(1)]));
    expect(env.requests.map(params)).toEqual([{ limit: ['50'] }]);
    fireEvent.change(screen.getByLabelText(text('scr08.history.family')), {
      target: { value: 'dwell' },
    });
    fireEvent.change(screen.getByLabelText(text('scr08.history.state')), {
      target: { value: 'discarded' },
    });
    fireEvent.change(screen.getByLabelText(text('scr08.history.zone')), {
      target: { value: zone().zone_id },
    });
    await act(async () => {
      await settle();
    });
    expect(env.requests).toHaveLength(1);
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.keyset.apply_filters') }),
    );
    await waitFor(() => {
      expect(env.requests).toHaveLength(2);
    }, WAIT);
    expect(params(env.requests[1] ?? new URL('http://x'))).toEqual({
      zone_id: [zone().zone_id],
      family: ['dwell'],
      lifecycle_state: ['discarded'],
      limit: ['50'],
    });
    // «Actualizar» (UI-20, a demanda) repite la primera página con los mismos filtros.
    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await waitFor(() => {
      expect(env.requests).toHaveLength(3);
    }, WAIT);
    expect(params(env.requests[2] ?? new URL('http://x'))).toEqual(
      params(env.requests[1] ?? new URL('http://x')),
    );
  });

  it('el periodo se manda en la hora de la planta, [desde, hasta)', async () => {
    const env = await opened(() => body([]));
    const from = screen.getByLabelText(text('scr08.history.from'));
    const to = screen.getByLabelText(text('scr08.history.to'));
    fireEvent.change(from, { target: { value: '01/03/2026' } });
    fireEvent.blur(from);
    fireEvent.change(to, { target: { value: '02/03/2026' } });
    fireEvent.blur(to);
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.keyset.apply_filters') }),
    );
    await waitFor(() => {
      expect(env.requests).toHaveLength(2);
    }, WAIT);
    // America/Bogota es UTC-5: el 1 de marzo empieza a las 05:00 UTC; el 2 termina el 3 a las 05.
    expect(params(env.requests[1] ?? new URL('http://x'))).toEqual({
      from: ['2026-03-01T05:00:00.000Z'],
      to: ['2026-03-03T05:00:00.000Z'],
      limit: ['50'],
    });
    await waitFor(() => {
      expect(document.querySelector('[data-part="empty"]')?.textContent).toBe(
        text('scr08.history.empty'),
      );
    }, WAIT);
  });

  it('pagina con el cursor opaco exacto del servidor y 50 por página', async () => {
    const env = await opened((url) =>
      url.searchParams.get('cursor') === null
        ? body([closed(1)], { next_cursor: 'cursor-del-servidor' })
        : body([discarded(2)]),
    );
    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    }, WAIT);
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.keyset.load_more') }),
    );
    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    }, WAIT);
    expect(env.requests.map(params)).toEqual([
      { limit: ['50'] },
      { cursor: ['cursor-del-servidor'], limit: ['50'] },
    ]);
    expect(document.querySelector('[data-part="end"]')?.textContent).toBe(
      text('components_structure.keyset.end'),
    );
  });

  it('una fila abre su ficha de vida, con el enlace o con Intro', async () => {
    await opened(() => body([closed(1), discarded(2)]));
    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    }, WAIT);
    const links = [...document.querySelectorAll('[data-part="timeline_link"]')];
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `/findings/${closed(1).finding_state_id}/timeline`,
      `/findings/${discarded(2).finding_state_id}/timeline`,
    ]);
    const [, second] = rows();
    if (second === undefined) throw new Error('falta la fila');
    fireEvent.keyDown(second, { key: 'Enter' });
    await waitFor(() => {
      expect(document.querySelector('[data-part="timeline_opened"]')).not.toBeNull();
    }, WAIT);
  });

  it('el enlace de la fila navega a la ficha de vida', async () => {
    await opened(() => body([closed(1)]));
    const link = await waitFor(() => {
      const found = document.querySelector<HTMLAnchorElement>('[data-part="timeline_link"]');
      if (found === null) throw new Error('sin enlace');
      return found;
    }, WAIT);
    fireEvent.click(link);
    await waitFor(() => {
      expect(document.querySelector('[data-part="timeline_opened"]')).not.toBeNull();
    }, WAIT);
  });

  it('una planta con la proyección atrasada se declara con su aviso', async () => {
    await opened(() =>
      body([], { partitions: [{ plant_id: PLANT, notice: 'projection_rebuilding' }] }),
    );
    const notices = await waitFor(() => {
      const found = document.querySelector('[data-part="partition_notices"]');
      if (found === null) throw new Error('sin avisos');
      return found;
    }, WAIT);
    expect(
      [...within(notices as HTMLElement).getAllByRole('listitem')].map((li) => li.textContent),
    ).toEqual([`Planta sintética: ${text('scr08.inbox.notice.projection_rebuilding')}`]);
  });

  it('desde la bandeja se llega con el enlace «Ver lo cerrado»', async () => {
    const { Wrapper } = await fixture({ path: '/findings', permissions: ['findings.read'] });
    render(<Inbox />, { wrapper: Wrapper });
    const link = await waitFor(() => {
      const found = document.querySelector('[data-part="closed_link"]');
      if (found === null) throw new Error('sin enlace');
      return found;
    }, WAIT);
    expect(link.textContent).toBe(text('scr08.history.link'));
    expect(link.getAttribute('href')).toBe('/findings/closed');
  });

  it('axe sin violaciones en el histórico con filas', async () => {
    const env = await opened(() => body([closed(1), discarded(2)]));
    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    }, WAIT);
    expect(await axe(env.view.container)).toHaveNoViolations();
  });
});
