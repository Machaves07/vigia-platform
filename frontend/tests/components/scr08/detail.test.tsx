/**
 * SCR-08, detalle auditado del hallazgo (IF-08 pasos 1 y 2; RV-12; BR-APP-36, 40, 54, 58 y 64).
 * Las aserciones comparan claves del catálogo, etiquetas de las unidades dueñas, códigos,
 * atributos y nombres accesibles exactos; nunca subcadenas de texto con datos (VIG-194).
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { settle } from '../../support/queryCache';
import {
  apiError,
  classification,
  detail,
  fixture,
  history,
  STANDARD_TEXT,
  validFinding,
  WAIT,
  ZONE_NAME,
} from './detail_support';

async function opened(env: Awaited<ReturnType<typeof fixture>>) {
  const view = env.view();
  await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
  await waitFor(() => {
    expect(env.cache.client.isFetching()).toBe(0);
  }, WAIT);
  return view;
}

function circuit(env: Awaited<ReturnType<typeof fixture>>) {
  return env.connection.getSnapshot().circuits.platform.state;
}

describe('SCR-08 detalle del hallazgo', { timeout: 60_000 }, () => {
  it('se pide una vez por apertura y nunca por refresco en 5 minutos simulados', async () => {
    const env = await fixture();
    await opened(env);
    expect(env.calls.detail).toBe(1);
    expect(env.calls.history).toBe(1);
    expect(env.calls.record).toBe(1);
    expect(env.calls.coverage).toHaveLength(1);
    for (let minute = 0; minute < 5; minute += 1) {
      await act(async () => {
        env.clock.advance(60_000);
        await settle();
      });
    }
    expect(env.calls.detail).toBe(1);
    expect(env.calls.history).toBe(1);
    expect(env.calls.record).toBe(1);
    expect(env.calls.coverage).toHaveLength(1);
    // El reproductor no pide su URL al montar: solo al pulsar reproducir (BR-APP-58).
    expect(env.calls.readUrl).toBe(0);
  });

  it('VIG-365: el único «Actualizar» vuelve a pedir las lecturas auditadas y «ahora» en el instante de pulsar', async () => {
    const env = await fixture();
    const { container } = await opened(env);
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }),
    ).toHaveLength(1);
    expect(container.querySelector('[data-policy]')?.getAttribute('data-policy')).toBe('on_demand');
    const first = env.calls.coverage[0]?.searchParams.get('instant');
    env.clock.advance(5_000);
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await waitFor(() => {
      expect(env.calls.coverage).toHaveLength(2);
      expect(env.cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect([env.calls.detail, env.calls.history, env.calls.record]).toEqual([2, 2, 2]);
    expect(env.calls.coverage.map((url) => url.searchParams.get('instant'))).toEqual([
      first,
      new Date(Date.parse(first ?? '') + 5_000).toISOString(),
    ]);
  });

  it('una apertura nueva vuelve a pedir el detalle una vez', async () => {
    const env = await fixture();
    const first = await opened(env);
    first.unmount();
    await opened(env);
    expect(env.calls.detail).toBe(2);
    expect(env.calls.history).toBe(2);
  });

  it('pinta el Finding validado: hora del nodo con desfase, cámara originaria y duración', async () => {
    const env = await fixture();
    await opened(env);
    const contract = await screen.findByText(
      text('scr08.detail.condition_duration'),
      { selector: '[data-part="contract"] dt' },
      WAIT,
    );
    const list = contract.closest('dl');
    expect(list).not.toBeNull();
    const finding = validFinding();
    expect(
      within(list as HTMLElement).getByText(String(finding.condition_duration_ms)),
    ).toBeDefined();
    const timestamp = (list as HTMLElement).querySelector('[data-part="timestamp"]');
    expect(timestamp?.querySelector('[data-part="node"]')).not.toBeNull();
    expect(timestamp?.querySelector('[data-part="offset"]')).not.toBeNull();
    const cameras = (list as HTMLElement).querySelectorAll('[data-part="camera"]');
    expect(cameras).toHaveLength(finding.cameras.length);
    expect(cameras[0]?.getAttribute('data-originating')).toBe('true');
    expect(
      [...cameras].filter((camera) => camera.getAttribute('data-originating') === 'true'),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: text('scr08.detail.summary') })).toBeDefined();
    expect(screen.getByText(ZONE_NAME)).toBeDefined();
    const signal = document.querySelector('[data-part="signal"]');
    expect(signal?.textContent).toBe(
      `${label('signal_role', 'energy')}: ${label('signal_value', 'asserted')}`,
    );
    expect(document.querySelectorAll('[data-part="clip"]')).toHaveLength(1);
  });

  it('un Finding con un campo inyectado no se pinta y la sección muestra el error de contrato', async () => {
    const env = await fixture();
    const injected = 'valor-inyectado-que-no-debe-verse-327';
    env.responses.record = () =>
      HttpResponse.json({ content: { ...validFinding(), person_label: injected } });
    const view = await opened(env);
    const error = await screen.findByText(text('scr08.detail.contract_error'), {}, WAIT);
    expect(error.getAttribute('role')).toBe('alert');
    expect(error.getAttribute('data-code')).toBe('contract_violation');
    expect(view.container.querySelector('[data-part="contract"]')).toBeNull();
    expect(view.container.querySelector('[data-part="camera"]')).toBeNull();
    expect(screen.queryByText(injected, { exact: false })).toBeNull();
    // El resto del detalle sigue: resumen, estándar e historial.
    expect(screen.getByText(STANDARD_TEXT)).toBeDefined();
    expect(screen.getByRole('region', { name: text('scr08.detail.history') })).toBeDefined();
  });

  it('muestra los dos semáforos, «en el momento» y «ahora», cuando difieren (BR-APP-36)', async () => {
    const env = await fixture();
    await opened(env);
    const atEvent = screen.getByRole('group', { name: text('scr08.detail.at_event') });
    const now = await screen.findByRole('group', { name: text('scr08.detail.now') }, WAIT);
    await waitFor(() => {
      expect(now.querySelector('[data-state]')?.getAttribute('data-state')).toBe('observable');
    }, WAIT);
    expect(atEvent.querySelector('[data-state]')?.getAttribute('data-state')).toBe('degraded');
    expect(within(atEvent).getByRole('heading').textContent).toBe(text('scr08.detail.at_event'));
    expect(within(now).getByRole('heading').textContent).toBe(text('scr08.detail.now'));
    // «Ahora» es el instante de la apertura según el reloj inyectado.
    expect(env.calls.coverage[0]?.searchParams.get('instant')).toBe('2026-01-01T12:00:00.000Z');
  });

  it('sin coverage.read no pide ni muestra «ahora»; «en el momento» sigue', async () => {
    const env = await fixture({ permissions: ['findings.read', 'evidence.read'] });
    await opened(env);
    expect(env.calls.coverage).toHaveLength(0);
    expect(screen.queryByRole('group', { name: text('scr08.detail.now') })).toBeNull();
    expect(screen.getByRole('group', { name: text('scr08.detail.at_event') })).toBeDefined();
  });

  it('aviso standard_text_unavailable: el resto se pinta, el circuito sigue cerrado y axe sin violaciones', async () => {
    const env = await fixture();
    env.responses.detail = () => {
      const value = detail();
      value.standard.declared_text = null;
      value.notices = [{ code: 'standard_text_unavailable', since: null, cause: null }];
      return HttpResponse.json(value);
    };
    const view = await opened(env);
    const standard = screen.getByRole('region', { name: text('scr08.detail.standard') });
    expect(standard.querySelector('[data-part="standard_unavailable"]')?.textContent).toBe(
      text('scr08.detail.standard_unavailable'),
    );
    expect(standard.querySelector('[data-part="declared_text"]')).toBeNull();
    expect(screen.getByRole('region', { name: text('scr08.detail.summary') })).toBeDefined();
    expect(screen.getAllByText(label('predicate_family', 'coexistence'))).not.toHaveLength(0);
    expect(document.querySelectorAll('[data-part="classification"]')).toHaveLength(2);
    expect(circuit(env)).toBe('closed');
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('503 loop_standard_text_unavailable degrada su sección sin abrir el circuito; el historial se pinta', async () => {
    const env = await fixture();
    env.responses.detail = () =>
      apiError('temporarily_unavailable', 503, 'loop_standard_text_unavailable');
    const view = env.view();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-part')).toBe('standard_text_unavailable');
    expect(within(alert).getByText(text('scr08.detail.standard_unavailable'))).toBeDefined();
    await waitFor(() => {
      expect(document.querySelectorAll('[data-part="classification"]')).toHaveLength(2);
    }, WAIT);
    expect(env.calls.detail).toBe(1);
    expect(circuit(env)).toBe('closed');
    expect(await axe(view.container)).toHaveNoViolations();
    env.responses.detail = () => HttpResponse.json(detail());
    fireEvent.click(within(alert).getByRole('button', { name: text('scr08.detail.retry') }));
    await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
    expect(env.calls.detail).toBe(2);
  });

  it('503 loop_gate_state_unavailable: fallo cerrado, el hallazgo no se muestra y el circuito sigue cerrado', async () => {
    const env = await fixture();
    env.responses.detail = () =>
      apiError('temporarily_unavailable', 503, 'loop_gate_state_unavailable');
    env.view();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-part')).toBe('gate_state_unavailable');
    expect(within(alert).getByText(text('scr08.detail.gate_unavailable'))).toBeDefined();
    expect(screen.queryByRole('region', { name: text('scr08.detail.summary') })).toBeNull();
    await waitFor(() => {
      expect(document.querySelectorAll('[data-part="classification"]')).toHaveLength(2);
    }, WAIT);
    expect(env.calls.record).toBe(0);
    expect(circuit(env)).toBe('closed');
  });

  it('un hallazgo fuera del alcance muestra la pantalla uniforme', async () => {
    const env = await fixture();
    env.responses.detail = () => apiError('not_found', 404);
    env.view();
    await screen.findByText(text('errors.not_available_in_scope'), {}, WAIT);
  });

  it('historial completo en el orden del servidor con motivo, autor, rol y fecha', async () => {
    const env = await fixture();
    env.responses.history = () =>
      HttpResponse.json(history([classification(1), classification(2), classification(3)]));
    await opened(env);
    const items = document.querySelectorAll('[data-part="classification"]');
    expect(items).toHaveLength(3);
    const first = items[0] as HTMLElement;
    expect(within(first).getByText(text('scr08.detail.classified_by'))).toBeDefined();
    expect(within(first).getByText('Coordinación sintética')).toBeDefined();
    expect(within(first).getByText(label('role', 'coordinator_sst'))).toBeDefined();
    expect(first.querySelector('[data-part="reason_text"]')?.textContent).toBe(
      classification(1).reason_text,
    );
    expect(first.querySelector('time')).not.toBeNull();
    expect([...items].map((item) => item.querySelector('strong')?.textContent)).toEqual(
      [1, 2, 3].map(
        (n) =>
          `${text('scr08.detail.sequence')} ${String(n)}: ${label('classification_outcome', 'confirmed')}`,
      ),
    );
  });

  it('aviso «acuerdo revocado desde <fecha>» y versión posterior del estándar', async () => {
    const env = await fixture();
    env.responses.detail = () => {
      const value = detail();
      value.notices = [
        { code: 'agreement_revoked', since: '2026-01-02T08:00:00.000Z', cause: null },
        { code: 'later_standard_version', since: null, cause: null },
      ];
      value.standard.superseded_since = '2026-01-03T00:00:00.000Z';
      value.standard.superseded_in_catalog_version = 9;
      return HttpResponse.json(value);
    };
    await opened(env);
    const revoked = document.querySelector('[data-notice="agreement_revoked"]');
    expect(revoked?.firstChild?.textContent).toBe(text('scr08.detail.agreement_revoked'));
    expect(revoked?.querySelector('time')?.getAttribute('dateTime')).not.toBeNull();
    expect(document.querySelector('[data-notice="later_standard_version"]')?.textContent).toBe(
      label('finding_detail_notice', 'later_standard_version'),
    );
    expect(document.querySelector('[data-part="superseded"] time')).not.toBeNull();
  });

  it('zona unipersonal: marca redondeada declarada y sin hora del nodo', async () => {
    const env = await fixture();
    env.responses.detail = () => {
      const value = detail();
      value.time_aggregated = true;
      value.aggregation_window_minutes = 60;
      return HttpResponse.json(value);
    };
    await opened(env);
    await waitFor(() => {
      expect(document.querySelector('[data-part="contract"]')).not.toBeNull();
    }, WAIT);
    expect(document.querySelector('[data-part="node"]')).toBeNull();
    expect(
      screen.getAllByText(`(${text('scr08.detail.aggregated')})`, { exact: false }),
    ).not.toHaveLength(0);
    expect(document.querySelector('[data-part="window"]')?.textContent).toBe(
      ` · 60 ${text('scr08.detail.aggregated_minutes')}`,
    );
  });

  it('sin el registro del expediente el resto del detalle sigue', async () => {
    const env = await fixture();
    env.responses.record = () => apiError('not_found', 404);
    await opened(env);
    await screen.findByText(text('scr08.detail.contract_unavailable'), {}, WAIT);
    expect(screen.getByText(STANDARD_TEXT)).toBeDefined();
    expect(document.querySelector('[data-part="contract"]')).toBeNull();
  });

  // --- VIG-359: recorrido guiado por el estado y resolución de la cola en el historial ---------

  // VIG-365: las claves del coordinador (las del servidor para el rol) para abrir cada paso.
  const COORDINATOR = [
    'findings.read',
    'evidence.read',
    'coverage.read',
    'findings.classify',
    'review_queue.resolve',
    'actions.manage',
    'closure_attachments.read',
    'findings.close_and_sign',
  ];

  /** [acceso, href, principal] de cada control del panel «Siguiente paso», en orden. */
  function panelControls() {
    const panel = screen.getByRole('region', { name: text('next_step.title') });
    return [...panel.querySelectorAll('[data-access]')].map((node) => [
      node.getAttribute('data-access'),
      node.getAttribute('href'),
      node.getAttribute('data-primary'),
    ]);
  }

  it.each([
    { state: 'in_review', kind: 'detection', expected: () => [['review_queue', '/review-queue']] },
    {
      state: 'received',
      kind: 'finding',
      expected: (id: string) => [['classify', `/findings/${id}/classify`]],
    },
    {
      state: 'open',
      kind: 'finding',
      expected: (id: string) => [
        ['action', `/findings/${id}/loop`],
        ['reclassify', `/findings/${id}/classify`],
      ],
    },
    ...['action_assigned', 'overdue'].map((state) => ({
      state,
      kind: 'finding',
      expected: (id: string) => [
        ['closure', `/findings/${id}/loop/closure`],
        ['unclosable', `/findings/${id}/loop/unclosable`],
      ],
    })),
    {
      state: 'closed',
      kind: 'finding',
      expected: (id: string) => [['reopening', `/findings/${id}/loop/reopening`]],
    },
    {
      state: 'unclosable',
      kind: 'finding',
      expected: (id: string) => [['unclosable_review', `/findings/${id}/loop/unclosable`]],
    },
  ])(
    'VIG-365: en $state, el panel da una sola acción principal, sin escribir la dirección',
    async ({ state, kind, expected }) => {
      const env = await fixture({ permissions: COORDINATOR });
      const value = { ...detail(), lifecycle_state: state, subject_kind: kind };
      env.responses.detail = () => HttpResponse.json(value);
      await opened(env);
      const controls = panelControls();
      expect(controls.map(([access, href]) => [access, href])).toEqual(
        expected(value.finding_state_id),
      );
      expect(controls.map(([, , primary]) => primary)).toEqual(
        controls.map((_, index) => (index === 0 ? 'true' : null)),
      );
      const suspends = document.querySelector('[data-part="suspends"]');
      expect(suspends === null).toBe(state !== 'open');
    },
  );

  it('VIG-365: título humano, chip del estado y paso a paso del ciclo', async () => {
    const env = await fixture({ permissions: COORDINATOR });
    const value = { ...detail(), lifecycle_state: 'action_assigned' };
    env.responses.detail = () => HttpResponse.json(value);
    await opened(env);
    const heading = screen.getByRole('heading', { level: 1 });
    expect(heading.textContent).not.toBe(text('scr08.detail.title'));
    const chip = document.querySelector('[data-part="status_chip"]');
    expect(chip?.getAttribute('data-state')).toBe('action_assigned');
    const steps = screen.getByRole('list', { name: text('lifecycle_steps.label') });
    expect(steps.querySelector('[aria-current="step"]')?.getAttribute('data-step')).toBe('action');
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }),
    ).toHaveLength(1);
  });

  it.each([
    { kind: 'detection', target: (subject: string) => subject },
    { kind: 'finding', target: () => 'ninguna' },
  ])(
    'VIG-359: en revisión, la cola recibe esa detección solo si el sujeto es $kind, sin consulta en la URL',
    async ({ kind, target }) => {
      const env = await fixture({ permissions: COORDINATOR });
      const value = { ...detail(), lifecycle_state: 'in_review', subject_kind: kind };
      env.responses.detail = () => HttpResponse.json(value);
      await opened(env);
      fireEvent.click(
        within(screen.getByRole('region', { name: text('next_step.title') })).getByRole('link', {
          name: text('next_step.access.review_queue'),
        }),
      );
      const queue = await screen.findByTestId('queue', {}, WAIT);
      expect(queue.textContent).toBe(target(value.subject_id));
      expect(queue.getAttribute('data-search')).toBe('');
    },
  );

  it('VIG-359: sin findings.classify no ofrece clasificar, que la ruta no abriría', async () => {
    const env = await fixture();
    env.responses.detail = () => HttpResponse.json({ ...detail(), lifecycle_state: 'received' });
    await opened(env);
    expect(screen.queryByRole('region', { name: text('next_step.title') })).toBe(null);
  });

  it('VIG-359: el historial muestra la resolución de la cola aparte de las clasificaciones', async () => {
    const env = await fixture({ permissions: COORDINATOR });
    const resolution = {
      resolution_id: '0190a1b2-cccc-7000-8000-000000000001',
      record_id: '0190a1b2-cccc-7000-8000-000000000002',
      outcome: 'review_discarded',
      reason_category: 'reflection_or_glare',
      reason_text: 'Reflejo sintético sobre la guarda de la zona, sin coexistencia.',
      resolved_at: '2026-01-01T12:30:00.000Z',
      resolved_by: {
        user_id: '0190a1b2-8888-7000-8000-000000000002',
        display_name: 'Revisión sintética',
        role_in_use: 'coordinator_sst',
      },
    };
    env.responses.history = () =>
      HttpResponse.json({ ...history([]), review_resolutions: [resolution] });
    await opened(env);
    const historyRegion = screen.getByRole('region', { name: text('scr08.detail.history') });
    const section = within(historyRegion).getByRole('region', {
      name: text('finding_journey.resolution'),
    });
    const items = section.querySelectorAll('[data-part="review_resolution"]');
    expect(items).toHaveLength(1);
    const item = items[0] as HTMLElement;
    expect(item.getAttribute('data-outcome')).toBe('review_discarded');
    expect(item.querySelector('strong')?.textContent).toBe(
      `${text('finding_journey.resolution_outcome')}: ${label('review_resolution_outcome', 'review_discarded')}`,
    );
    expect(
      within(item).getByText(
        `${text('finding_journey.resolution_reason')}: ${label('reason_category_false_positive', 'reflection_or_glare')}`,
      ),
    ).toBeDefined();
    expect(item.querySelector('[data-part="reason_text"]')?.textContent).toBe(
      resolution.reason_text,
    );
    expect(item.querySelector('time')?.getAttribute('dateTime')).not.toBeNull();
    const author = item.querySelector('[data-role-in-use]');
    expect(author?.getAttribute('data-role-in-use')).toBe('coordinator_sst');
    expect(author?.textContent).toBe(
      `${text('finding_journey.resolved_by')}: Revisión sintética ${text('finding_journey.role')} ${label('role', 'coordinator_sst')}`,
    );
    // Distinguida de las clasificaciones: ninguna resolución cuenta como clasificación.
    expect(historyRegion.querySelectorAll('[data-part="classification"]')).toHaveLength(0);
    expect(within(historyRegion).getByText(text('scr08.detail.history_empty'))).toBeDefined();
  });
});
