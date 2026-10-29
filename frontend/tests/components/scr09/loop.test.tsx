import { render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { LoopView } from '@/pages/scr09/loop/LoopView';
import { LIFECYCLE_STATES } from '@/ports/scr09Classify';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { readAppYaml } from '../../support/openapiAjv';
import { classification, detail, FINDING, INSTANT, renderScreen, WAIT } from './classify_support';

const SCHEMA_STATES = (
  readAppYaml().components as { schemas: { FindingLifecycleState: { enum: string[] } } }
).schemas.FindingLifecycleState.enum;

function stateOf(container: HTMLElement): string | null {
  return container.querySelector('[data-part="lifecycle_state"]')?.textContent ?? null;
}

function serveDetail(lifecycle_state: string) {
  let reads = 0;
  server.use(
    // Cerrado o incerrable, el lazo lee además su expediente (la firma del cierre, VIG-365).
    http.get('http://localhost/ledger/records', () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    http.get(`http://localhost/loop/findings/${FINDING}`, () => {
      reads++;
      return HttpResponse.json(
        detail({
          lifecycle_state,
          classifications: [classification(1), classification(2, 'false_positive')],
          classification_count: 2,
          actions: [
            {
              action_id: '0190a1b2-7777-7000-8000-000000000001',
              record_id: '0190a1b2-7777-7000-8000-000000000002',
              action_type: 'engineering',
              action_description: 'Instalar una barrera fija en el acceso lateral de la prensa',
              expected_outcome: null,
              state: 'superseded',
              assigned_at: INSTANT,
              due_at: INSTANT,
              counts_as_overdue: false,
              root_cause_analysis_id: null,
            },
          ],
        }),
      );
    }),
  );
  return { reads: () => reads };
}

const ALL = [
  'findings.read',
  'findings.classify',
  'actions.manage',
  'closure_attachments.read',
  'findings.close_and_sign',
  'hierarchy.read',
];

describe('SCR-09 lazo', { timeout: 40_000 }, () => {
  it.each([
    {
      name: 'en plazo',
      state: 'assigned',
      due: '2026-12-08T12:00:00.000Z',
      counts: true,
      overdue: false,
    },
    {
      name: 'vencida',
      state: 'overdue',
      due: '2025-12-08T12:00:00.000Z',
      counts: true,
      overdue: true,
    },
    {
      name: 'suspendida',
      state: 'assigned',
      due: '2025-12-08T12:00:00.000Z',
      counts: false,
      overdue: false,
    },
  ])(
    'acción $name: distingue vencimiento y cómputo suspendido',
    ({ state, due, counts, overdue }) => {
      const { container } = render(
        <LoopView
          detail={detail({
            actions: [
              {
                action_id: FINDING,
                record_id: FINDING,
                action_type: 'engineering',
                action_description: 'Instalar una barrera fija en el acceso lateral',
                expected_outcome: null,
                state,
                assigned_at: INSTANT,
                due_at: due,
                counts_as_overdue: counts,
                root_cause_analysis_id: null,
              },
            ],
          })}
          zone={null}
          timeZone="UTC"
        />,
      );
      const entry = container.querySelector('[data-part="action"]');
      expect(entry?.textContent).not.toContain('Cuenta como vencida');
      expect(/vencida/iu.test(entry?.textContent ?? '')).toBe(overdue);
      expect(
        entry?.textContent.includes('No cuenta para vencimientos mientras el lazo está suspendido'),
      ).toBe(!counts);
    },
  );

  it('la lista del puerto son los nueve estados del esquema generado', () => {
    expect(LIFECYCLE_STATES).toHaveLength(9);
    expect([...LIFECYCLE_STATES].sort()).toEqual([...SCHEMA_STATES].sort());
  });

  it.each(SCHEMA_STATES)('estado %s: con su etiqueta, nunca «desconocido»', (state) => {
    const { container } = render(
      <LoopView detail={detail({ lifecycle_state: state })} zone={null} timeZone="UTC" />,
    );
    expect(stateOf(container)).toBe(label('finding_lifecycle_state', state));
    expect(stateOf(container)).not.toBe(text('common.unknown'));
  });

  it('un estado fuera de la lista se presenta como «desconocido»', () => {
    const { container } = render(
      <LoopView detail={detail({ lifecycle_state: 'zone_cleared' })} zone={null} timeZone="UTC" />,
    );
    expect(stateOf(container)).toBe(text('common.unknown'));
  });

  it('abre cargado (VIG-365); registros en orden, zona del escalamiento y, cerrado, solo la reapertura', async () => {
    const served = serveDetail('closed');
    const { container, cache } = await renderScreen('loop', { permissions: ALL });
    const panel = await screen.findByRole('region', { name: text('next_step.title') }, WAIT);
    expect(screen.queryByRole('button', { name: text('scr09.loop.load') })).toBeNull();
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
    expect(served.reads()).toBe(1);
    expect(stateOf(container)).toBe(label('finding_lifecycle_state', 'closed'));
    expect(
      [...container.querySelectorAll('[data-part="classification"] [data-part="reason_text"]')].map(
        (node) => node.textContent,
      ),
    ).toEqual([classification(1).reason_text, classification(2).reason_text]);
    expect(container.querySelectorAll('[data-part="action"]')).toHaveLength(1);
    expect(screen.getByText(`${text('scr09.loop.zone')}: Zona sintética de prensas`)).toBeTruthy();
    expect(
      [...panel.querySelectorAll('a')].map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([[text('next_step.access.reopening'), `/findings/${FINDING}/loop/reopening`]]);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('sin claves de gestión no hay pasos ni formulario de acción', async () => {
    serveDetail('open');
    await renderScreen('loop', { permissions: ['findings.read'] });
    await screen.findByRole('list', { name: text('scr09.loop.actions') }, WAIT);
    expect(screen.queryByRole('region', { name: text('next_step.title') })).toBeNull();
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
  });
});
