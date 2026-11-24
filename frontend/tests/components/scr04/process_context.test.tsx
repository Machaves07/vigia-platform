/**
 * SCR-04 · Valores de proceso (TASK-594, A-129): el administrador declara una analógica y una de
 * estado, ve en vivo el error de la escala plana, guarda y ve el acuse. Comprobaciones por clave de
 * texto y `data-part`; datos sintéticos con las escalas de Pope.
 */
import { fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr04/parameters';
import {
  processBody,
  processDraft,
  processIssues,
  type DeclaredSignal,
} from '@/pages/scr04/parameters/ProcessContextFields';
import { bodyIssues, screenLabel, type Catalog } from '@/ports/scr04Parameters';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { catalog, REASON, renderPage, WAIT, ZONE } from './support';

const ENERGY = '0190a1b2-4444-7000-8000-000000000101';
const JUMBO_SIGNAL = '0190a1b2-4444-7000-8000-000000000102';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
const SIGNALS: DeclaredSignal[] = [
  { signal_id: ENERGY, code: 'SG-ENERGIA', description_es: 'Energía de la enrolladora' },
  { signal_id: JUMBO_SIGNAL, code: 'SG-JUMBO', description_es: 'Jumbo en posición' },
];

function withSignals(version = 1): Catalog {
  const base = catalog(version);
  return {
    ...base,
    catalog: {
      ...base.catalog,
      signals: SIGNALS.map((signal, index) => ({
        ...signal,
        role: index === 0 ? 'energy' : 'guard',
        asserted_level: 'high',
        source: { reader: 'waveshare', channel: 4 + index },
      })),
    },
  };
}

async function ready() {
  const view = await renderPage(<Page />, undefined, withSignals());
  await screen.findByRole('form', { name: text('scr04.parameters.title') }, WAIT);
  fireEvent.change(screen.getByRole('combobox', { name: text('scr04.parameters.parameter') }), {
    target: { value: 'process_context' },
  });
  return view;
}

function variables(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-part="process_variable"]'));
}
function field(scope: HTMLElement, key: string, role = 'textbox'): HTMLElement {
  return within(scope).getByRole(role, { name: text(`scr04.parameters.${key}`) });
}
function set(scope: HTMLElement, key: string, value: string, role = 'textbox') {
  fireEvent.change(field(scope, key, role), { target: { value } });
}
function issueKeys(scope: ParentNode = document): string[] {
  return Array.from(scope.querySelectorAll('[data-part="process_issue"]')).map(
    (node) => node.getAttribute('data-text-key') ?? '',
  );
}
function add() {
  fireEvent.click(screen.getByRole('button', { name: text('scr04.parameters.process_add') }));
}
function reason() {
  fireEvent.change(
    screen.getByRole('textbox', {
      name: `${text('scr04.parameters.reason')} (${text('components_forms.reason.required')})`,
    }),
    { target: { value: REASON } },
  );
}
function submit() {
  fireEvent.submit(screen.getByRole('form', { name: text('scr04.parameters.title') }));
}

describe('SCR-04 valores de proceso', { timeout: 60_000 }, () => {
  it('declara una analógica y una de estado, ve la escala plana en vivo, guarda y ve el acuse', async () => {
    const view = await ready();
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/process-context', async ({ request, params }) => {
        expect(params.zone).toBe(ZONE);
        calls.push(await request.json());
        return HttpResponse.json({ ...withSignals(2), changed_fields: ['process_context'] });
      }),
    );
    expect(
      screen.getByRole<HTMLInputElement>('spinbutton', {
        name: text('scr04.parameters.process_before_seconds'),
      }).value,
    ).toBe('30');

    add();
    const [speed] = variables();
    if (speed === undefined) throw new Error('sin variable');
    expect(issueKeys()).toEqual([]);
    set(speed, 'process_code', 'vel-maq');
    expect((field(speed, 'process_code') as HTMLInputElement).value).toBe('VEL-MAQ');
    set(speed, 'process_description', 'Velocidad de la máquina');
    set(speed, 'process_reader', 'waveshare');
    set(speed, 'process_at_20_ma', '0', 'spinbutton');
    expect(issueKeys(speed)).toEqual(['scr04.parameters.process_scale_flat']);
    expect(field(speed, 'process_at_20_ma', 'spinbutton').getAttribute('aria-invalid')).toBe(
      'true',
    );
    set(speed, 'process_at_20_ma', '2000', 'spinbutton');
    expect(issueKeys(speed)).toEqual([]);

    add();
    const meters = variables()[1];
    if (meters === undefined) throw new Error('sin variable');
    // Prellenada: el lector y el canal siguiente de la última analógica.
    expect((field(meters, 'process_reader') as HTMLInputElement).value).toBe('waveshare');
    expect((field(meters, 'process_channel', 'spinbutton') as HTMLInputElement).value).toBe('1');
    set(meters, 'process_kind', 'binary', 'combobox');
    set(meters, 'process_code', 'JUMBO-POS');
    set(meters, 'process_description', 'Jumbo en posición');
    const options = within(field(meters, 'process_signal', 'combobox'))
      .getAllByRole<HTMLOptionElement>('option')
      .map((option) => option.value);
    expect(options).toEqual([ENERGY, JUMBO_SIGNAL]);
    set(meters, 'process_signal', JUMBO_SIGNAL, 'combobox');
    set(meters, 'process_states', 'in_position_out', 'combobox');
    expect(
      within(field(meters, 'process_states', 'combobox')).getByRole('option', {
        name: screenLabel('process_state_pair', 'in_position_out'),
      }),
    ).toBeTruthy();

    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toHaveLength(1);
    const sent = calls[0] as { variables: { variable_id: string }[] };
    expect(sent.variables.map((variable) => UUID.test(variable.variable_id))).toEqual([true, true]);
    expect(calls).toEqual([
      {
        before_ms: 30_000,
        variables: [
          {
            variable_id: sent.variables[0]?.variable_id,
            code: 'VEL-MAQ',
            kind: 'analog',
            description_es: 'Velocidad de la máquina',
            source: { reader: 'waveshare', channel: 0 },
            unit: 'm_per_min',
            scale: { at_4_ma: 0, at_20_ma: 2000 },
            decimals: 0,
          },
          {
            variable_id: sent.variables[1]?.variable_id,
            code: 'JUMBO-POS',
            kind: 'binary',
            description_es: 'Jumbo en posición',
            signal_id: JUMBO_SIGNAL,
            states: 'in_position_out',
          },
        ],
        reason_es: REASON,
      },
    ]);
    expect(
      view.container.querySelector('[data-role-in-use]')?.getAttribute('data-role-in-use'),
    ).toBe('administrator');
    expect(
      (await axe(view.container)).violations.filter(
        (item) => item.impact === 'serious' || item.impact === 'critical',
      ),
    ).toEqual([]);
  });

  it('no envía nada mientras queda un campo inválido y lo señala al intentar guardar', async () => {
    await ready();
    let writes = 0;
    server.use(
      http.put('http://localhost/zones/:zone/process-context', () => {
        writes += 1;
        return HttpResponse.json(withSignals(2));
      }),
    );
    add();
    reason();
    submit();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.textContent).toBe(text('scr04.parameters.process_fix_fields'));
    expect(issueKeys()).toEqual([
      'scr04.parameters.process_code_format',
      'scr04.parameters.process_description_invalid',
      'scr04.parameters.process_reader_format',
      'scr04.parameters.process_scale_number',
    ]);
    expect(writes).toBe(0);
  });

  it('prellena la versión vigente y retira el bloque con la lista vacía', async () => {
    const current = withSignals();
    current.catalog = {
      ...current.catalog,
      process_context: {
        before_ms: 10_000,
        variables: [
          {
            variable_id: '0190a1b2-5555-7000-8000-000000000101',
            code: 'MET-JUMBO',
            kind: 'analog',
            description_es: 'Metros en bobina',
            source: { reader: 'waveshare', channel: 1 },
            unit: 'm',
            scale: { at_4_ma: 0, at_20_ma: 50_000 },
            decimals: 0,
          },
        ],
      },
    };
    await renderPage(<Page />, undefined, current);
    await screen.findByRole('form', { name: text('scr04.parameters.title') }, WAIT);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr04.parameters.parameter') }), {
      target: { value: 'process_context' },
    });
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/process-context', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(withSignals(2));
      }),
    );
    const [meters] = variables();
    if (meters === undefined) throw new Error('sin variable');
    expect((field(meters, 'process_at_20_ma', 'spinbutton') as HTMLInputElement).value).toBe(
      '50000',
    );
    fireEvent.click(
      within(meters).getByRole('button', { name: text('scr04.parameters.process_remove') }),
    );
    expect(variables()).toEqual([]);
    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toEqual([{ before_ms: 10_000, variables: [], reason_es: REASON }]);
  });

  it('las reglas en vivo son las del contrato: señal no declarada, código repetido y ventana', () => {
    const draft = processDraft(withSignals());
    const binary = {
      variable_id: '0190a1b2-5555-7000-8000-000000000102',
      code: 'JUMBO-POS',
      kind: 'binary' as const,
      description_es: 'Jumbo en posición',
      reader: '',
      channel: '',
      unit: 'm_per_min' as const,
      at_4_ma: '',
      at_20_ma: '',
      decimals: '',
      signal_id: '0190a1b2-9999-7000-8000-000000000101',
      states: 'in_position_out' as const,
    };
    const issues = processIssues(
      { before_seconds: '121', variables: [binary, { ...binary, signal_id: JUMBO_SIGNAL }] },
      SIGNALS,
    );
    expect([...issues.entries()]).toEqual([
      ['before_seconds', 'scr04.parameters.process_before_range'],
      ['0.code', 'scr04.parameters.process_code_repeated'],
      ['0.signal_id', 'scr04.parameters.process_signal_undeclared'],
      ['1.code', 'scr04.parameters.process_code_repeated'],
    ]);
    expect(draft).toEqual({ before_seconds: '30', variables: [] });
    const body = processBody({
      before_seconds: '120',
      variables: [{ ...binary, signal_id: ENERGY }],
    });
    expect(
      bodyIssues('process_context_zones__zone_id__process_context_put', {
        ...body,
        reason_es: REASON,
      }),
    ).toEqual([]);
  });

  it('el selector de señales solo ofrece las declaradas, sin señales no hay opción válida', async () => {
    await renderPage(<Page />, undefined, catalog());
    await screen.findByRole('form', { name: text('scr04.parameters.title') }, WAIT);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr04.parameters.parameter') }), {
      target: { value: 'process_context' },
    });
    add();
    const [variable] = variables();
    if (variable === undefined) throw new Error('sin variable');
    set(variable, 'process_kind', 'binary', 'combobox');
    const options = within(field(variable, 'process_signal', 'combobox'))
      .getAllByRole<HTMLOptionElement>('option')
      .map((option) => option.value);
    expect(options).toEqual(['']);
    expect(issueKeys(variable)).toEqual([]);
    submit();
    expect(issueKeys(variable)).toContain('scr04.parameters.process_signal_undeclared');
  });
  it('no deja retirar una señal que cita una variable de estado publicada y dice por qué', async () => {
    const current = withSignals();
    current.catalog = {
      ...current.catalog,
      process_context: {
        before_ms: 30_000,
        variables: [
          {
            variable_id: '0190a1b2-5555-7000-8000-000000000103',
            code: 'JUMBO-POS',
            kind: 'binary',
            description_es: 'Jumbo en posición',
            signal_id: JUMBO_SIGNAL,
            states: 'in_position_out',
          },
        ],
      },
    };
    await renderPage(<Page />, undefined, current);
    await screen.findByRole('form', { name: text('scr04.parameters.title') }, WAIT);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr04.parameters.parameter') }), {
      target: { value: 'signals' },
    });
    let writes = 0;
    server.use(
      http.put('http://localhost/zones/:zone/signals', () => {
        writes += 1;
        return HttpResponse.json(withSignals(2));
      }),
    );
    const removers = screen.getAllByRole('button', {
      name: text('scr04.parameters.remove_signal'),
    });
    const last = removers.at(-1);
    if (last === undefined) throw new Error('sin señales');
    fireEvent.click(last);
    reason();
    submit();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.textContent).toBe(text('scr04.parameters.signal_cited_by_process'));
    expect(writes).toBe(0);
  });
});
