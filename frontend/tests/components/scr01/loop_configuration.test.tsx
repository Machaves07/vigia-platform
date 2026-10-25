/** SUP-U05-38/39: consulta y edición por planta, rangos generados y motivo obligatorio. */
import { act, fireEvent, screen, render, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr01/hierarchy';
import { LoopConfigurationView } from '@/pages/scr01/hierarchy/LoopConfiguration';
import { LOOP_FIELDS, type ConfigurationChange } from '@/ports/scr01Hierarchy';
import loopSchema from '../../../openapi/bodies/change_configuration_loop_plants__plant_id__configuration_put.json';
import loopLabels from '../../../../backend/resources/labels.loop.es.json';
import { forbiddenRendered } from '../../support/viewProperties';
import { text } from '@/texts';
import { server } from '../../msw/server';
import {
  apiError,
  configuration,
  hierarchy,
  OTHER_PLANT,
  PLANT,
  renderAdmin,
  WAIT,
} from './hierarchySupport';

async function ready() {
  const view = await renderAdmin(<Page />);
  await screen.findByRole('heading', { name: 'PLANTA-SINT — Planta sintética' }, WAIT);
  return view;
}

const REASON = 'Cambio sintético de configuración de la planta.';
const reasonName = () =>
  `${text('scr01_hierarchy.reason')} (${text('components_forms.reason.required')})`;

async function editReady() {
  const view = await ready();
  const calls: { plant: string; body: ConfigurationChange }[] = [];
  let current = configuration();
  server.use(
    http.get('http://localhost/loop/plants/:plantId/configuration', () =>
      HttpResponse.json(current),
    ),
    http.put('http://localhost/loop/plants/:plantId/configuration', async ({ params, request }) => {
      const body = (await request.json()) as ConfigurationChange;
      calls.push({ plant: String(params.plantId), body });
      current = {
        ...current,
        ...body,
        business_calendar: body.business_calendar ?? current.business_calendar,
        version: current.version + 1,
        change_reason: body.reason ?? current.change_reason,
      } as typeof current;
      return HttpResponse.json(current);
    }),
  );
  fireEvent.click(screen.getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }));
  const form = await screen.findByRole('form', { name: text('scr01_hierarchy.edit_loop') }, WAIT);
  return { ...view, form, calls };
}

const boundaries = LOOP_FIELDS.flatMap((field) => {
  const schema = loopSchema.body.$defs.ConfigurationChangeBody.properties[field].anyOf.find(
    (branch) => branch.type === 'integer',
  );
  if (
    schema === undefined ||
    typeof schema.minimum !== 'number' ||
    typeof schema.maximum !== 'number'
  )
    throw new Error('Falta rango generado de prueba');
  return [
    { field, edge: 'mínimo', value: schema.minimum, valid: true },
    { field, edge: 'máximo', value: schema.maximum, valid: true },
    { field, edge: 'mínimo menos uno', value: schema.minimum - 1, valid: false },
    { field, edge: 'máximo más uno', value: schema.maximum + 1, valid: false },
  ];
});

describe('SCR-01 configuración del lazo', { timeout: 60_000 }, () => {
  it('proyecta solo parámetros conocidos y no pinta identidades o campos extra inyectados', () => {
    const hostile = {
      ...configuration(),
      person_id: 'identidad-sintetica-prohibida',
      email: 'identidad@ejemplo.test',
      employee_count: 19,
      business_calendar: {
        ...configuration().business_calendar,
        face_url: 'https://ejemplo.test/rostro',
      },
    };
    const { container } = render(<LoopConfigurationView configuration={hostile} />);
    expect(
      forbiddenRendered(
        {
          screen: 'SCR-01',
          view: 'RV-03',
          allowedFields: [
            ...LOOP_FIELDS,
            'timezone',
            'business_calendar',
            'working_weekdays',
            'non_working_dates',
          ],
        },
        hostile,
        container,
      ),
    ).toEqual([]);
    expect(container.querySelectorAll('dt')).toHaveLength(20);
  });
  it('consulta solo la planta abierta y presenta los 19 parámetros, calendario y zona horaria informativa', async () => {
    const view = await ready();
    const reads: string[] = [];
    server.use(
      http.get('http://localhost/loop/plants/:plantId/configuration', ({ params }) => {
        reads.push(String(params.plantId));
        return HttpResponse.json(configuration(String(params.plantId)));
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(reads).toEqual([]);
    fireEvent.click(
      screen.getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }),
    );
    const region = await screen.findByRole(
      'region',
      { name: text('scr01_hierarchy.loop_configuration') },
      WAIT,
    );
    await screen.findByText(
      text('scr01_hierarchy.reminder_interval_hours'),
      { selector: 'dt' },
      WAIT,
    );
    expect(region.querySelectorAll('dt')).toHaveLength(20);
    expect(region.querySelectorAll('input[name="timezone"],select[name="timezone"]')).toHaveLength(
      0,
    );
    expect(reads).toEqual([PLANT]);
    expect(screen.getByText(text('scr01_hierarchy.monday'), { selector: 'li' }).tagName).toBe('LI');
    expect(screen.getByText('2026-03-23', { selector: 'li' }).tagName).toBe('LI');
    expect(
      view.cache.client
        .getQueryCache()
        .getAll()
        .find((query) => query.queryKey[1] === 'GET /loop/plants/{plant_id}/configuration')
        ?.queryKey[4],
    ).toEqual({ plant_id: PLANT });
  });

  it('cambiar de planta no reutiliza la configuración de la anterior', async () => {
    const { cache } = await ready();
    const data = hierarchy();
    const first = data.plants[0];
    if (first === undefined) throw new Error('Falta planta sintética');
    server.use(
      http.get('http://localhost/hierarchy', () =>
        HttpResponse.json({
          ...data,
          plants: [
            ...data.plants,
            {
              ...first,
              plant_id: OTHER_PLANT,
              code: 'OTRA',
              name: 'Otra planta sintética',
              zones: [],
            },
          ],
        }),
      ),
      http.get('http://localhost/loop/plants/:plantId/configuration', ({ params }) =>
        HttpResponse.json({
          ...configuration(String(params.plantId)),
          classification_deadline_business_days: params.plantId === PLANT ? 2 : 29,
        }),
      ),
    );
    await act(async () => {
      await cache.client.refetchQueries();
    });
    const buttons = screen.getAllByRole('button', {
      name: text('scr01_hierarchy.loop_configuration'),
    });
    fireEvent.click(buttons[0] ?? screen.getByRole('main'));
    const firstField = await screen.findByRole<HTMLInputElement>(
      'spinbutton',
      { name: text('scr01_hierarchy.classification_deadline_business_days') },
      WAIT,
    );
    expect(firstField.value).toBe('2');
    fireEvent.click(buttons[1] ?? screen.getByRole('main'));
    const nextField = await screen.findByRole<HTMLInputElement>(
      'spinbutton',
      { name: text('scr01_hierarchy.classification_deadline_business_days') },
      WAIT,
    );
    expect(nextField.value).toBe('29');
    expect(firstField.isConnected).toBe(false);
  });

  it('un 404 usa el estado uniforme, sin distinguir recurso inexistente de fuera del alcance', async () => {
    await ready();
    server.use(
      http.get('http://localhost/loop/plants/:plantId/configuration', () =>
        apiError('not_found', 404),
      ),
    );
    fireEvent.click(
      screen.getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }),
    );
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
  });

  it('axe sin violaciones serias en la lectura y edición de configuración', async () => {
    const { container } = await ready();
    fireEvent.click(
      screen.getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }),
    );
    await screen.findByText(
      text('scr01_hierarchy.reminder_interval_hours'),
      { selector: 'dt' },
      WAIT,
    );
    const result = await axe(container);
    expect(
      result.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
    ).toEqual([]);
  });

  it('sin motivo no envía el cambio y lo ancla a ReasonField', async () => {
    const { form, calls } = await editReady();
    fireEvent.change(
      within(form).getByRole('spinbutton', {
        name: text('scr01_hierarchy.classification_deadline_business_days'),
      }),
      { target: { value: '4' } },
    );
    fireEvent.submit(form);
    await act(async () => {
      await Promise.resolve();
    });
    expect(calls).toHaveLength(0);
    expect(
      within(form).getByRole('button', { name: text('scr01_hierarchy.save_loop') }),
    ).toHaveProperty('disabled', false);
    const reason = within(form).getByRole('textbox', { name: reasonName() });
    expect(reason.getAttribute('aria-invalid')).toBe('true');
    expect(within(form).getByText(text('validation.required')).getAttribute('data-text-key')).toBe(
      'validation.required',
    );
  });

  it.each([10, 500])('acepta un motivo de %i caracteres, borde del esquema', async (length) => {
    const { form, calls } = await editReady();
    const reason = 'x'.repeat(length);
    fireEvent.change(
      within(form).getByRole('spinbutton', {
        name: text('scr01_hierarchy.classification_deadline_business_days'),
      }),
      { target: { value: '4' } },
    );
    fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
      target: { value: reason },
    });
    fireEvent.submit(form);
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(calls).toEqual([
      { plant: PLANT, body: { reason, classification_deadline_business_days: 4 } },
    ]);
    expect(
      within(form).getByRole('textbox', { name: reasonName() }).getAttribute('aria-invalid'),
    ).toBe('false');
  });

  it.each(boundaries)('$field: $edge ($value), válido=$valid', async ({ field, value, valid }) => {
    const { form, calls } = await editReady();
    const input = within(form).getByRole<HTMLInputElement>('spinbutton', {
      name: text(`scr01_hierarchy.${field}`),
    });
    fireEvent.change(input, { target: { value: String(value) } });
    fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
      target: { value: REASON },
    });
    fireEvent.submit(form);
    if (valid) {
      await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
      expect(calls).toEqual([{ plant: PLANT, body: { [field]: value, reason: REASON } }]);
    } else {
      await act(async () => {
        await Promise.resolve();
      });
      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(
        document
          .getElementById(input.getAttribute('aria-describedby') ?? '')
          ?.getAttribute('data-text-key'),
      ).toBe('validation.out_of_range');
      expect(calls).toHaveLength(0);
    }
  });

  it.each(['corto', 'x'.repeat(501)])(
    'rechaza motivo fuera de la longitud generada antes de enviar (%j)',
    async (reason) => {
      const { form, calls } = await editReady();
      fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
        target: { value: reason },
      });
      fireEvent.submit(form);
      await act(async () => {
        await Promise.resolve();
      });
      expect(
        within(form).getByRole('textbox', { name: reasonName() }).getAttribute('aria-invalid'),
      ).toBe('true');
      expect(calls).toHaveLength(0);
    },
  );

  it('calendario declarado con motivo; sin días laborables no se envía y el error se ancla al grupo', async () => {
    const { form, calls } = await editReady();
    const group = within(form).getByRole('group', {
      name: text('scr01_hierarchy.working_weekdays'),
    });
    for (const checkbox of within(group).getAllByRole<HTMLInputElement>('checkbox'))
      if (checkbox.checked) fireEvent.click(checkbox);
    fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
      target: { value: REASON },
    });
    fireEvent.submit(form);
    await act(async () => {
      await Promise.resolve();
    });
    expect(calls).toHaveLength(0);
    expect(group.getAttribute('aria-invalid')).toBe('true');
    expect(
      document
        .getElementById(group.getAttribute('aria-describedby') ?? '')
        ?.getAttribute('data-text-key'),
    ).toBe('validation.too_few_items');
    fireEvent.click(within(group).getByRole('checkbox', { name: text('scr01_hierarchy.monday') }));
    fireEvent.change(
      within(group).getByRole('textbox', { name: text('scr01_hierarchy.non_working_dates_input') }),
      { target: { value: '2026-05-01\n2026-12-25' } },
    );
    fireEvent.submit(form);
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(calls).toEqual([
      {
        plant: PLANT,
        body: {
          reason: REASON,
          business_calendar: {
            working_weekdays: [1],
            non_working_dates: ['2026-05-01', '2026-12-25'],
          },
        },
      },
    ]);
  });

  it('el 400 invalid_request/detail_code del servidor prevalece; conserva motivo y parámetros sin reintentar', async () => {
    const { form, clock } = await editReady();
    let writes = 0;
    server.use(
      http.put('http://localhost/loop/plants/:plantId/configuration', () => {
        writes += 1;
        return apiError('invalid_request', 400, 'loop_deadline_out_of_range');
      }),
    );
    // Esta relación pertenece al dominio: 1000 está dentro del rango de sample_min.
    const input = within(form).getByRole<HTMLInputElement>('spinbutton', {
      name: text('scr01_hierarchy.sample_min'),
    });
    fireEvent.change(input, { target: { value: '1000' } });
    fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
      target: { value: REASON },
    });
    fireEvent.submit(form);
    const alert = await within(form).findByRole('alert', {}, WAIT);
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      loopLabels.loop_detail_code.loop_deadline_out_of_range,
    );
    expect(input.value).toBe('1000');
    expect(
      within(form).getByRole<HTMLTextAreaElement>('textbox', { name: reasonName() }).value,
    ).toBe(REASON);
    expect(
      document.getElementById(form.getAttribute('aria-describedby') ?? '')?.contains(alert),
    ).toBe(true);
    await act(async () => {
      clock.advance(60_000);
      await Promise.resolve();
    });
    expect(writes).toBe(1);
  });

  it('el rechazo del motivo se ancla a ReasonField y sustituye su mensaje local', async () => {
    const { form } = await editReady();
    server.use(
      http.put('http://localhost/loop/plants/:plantId/configuration', () =>
        apiError('invalid_request', 400, 'loop_configuration_change_reason_required'),
      ),
    );
    const reason = within(form).getByRole('textbox', { name: reasonName() });
    fireEvent.change(reason, { target: { value: REASON } });
    fireEvent.submit(form);
    const alert = await within(form).findByRole('alert', {}, WAIT);
    fireEvent.change(reason, { target: { value: 'x' } });
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      loopLabels.loop_detail_code.loop_configuration_change_reason_required,
    );
    expect(within(form).queryByText(text('validation.too_short'))).toBeNull();
    expect(reason.getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
  });

  it('un refresco mientras hay borrador no convierte parámetros intactos en cambios', async () => {
    const { form, cache, calls } = await editReady();
    const input = within(form).getByRole('spinbutton', {
      name: text('scr01_hierarchy.classification_deadline_business_days'),
    });
    fireEvent.change(input, { target: { value: '4' } });
    server.use(
      http.get('http://localhost/loop/plants/:plantId/configuration', () =>
        HttpResponse.json({ ...configuration(), sample_percent: 20, version: 2 }),
      ),
    );
    await act(async () => {
      await cache.client.refetchQueries();
    });
    fireEvent.change(within(form).getByRole('textbox', { name: reasonName() }), {
      target: { value: REASON },
    });
    fireEvent.submit(form);
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(calls).toEqual([
      { plant: PLANT, body: { classification_deadline_business_days: 4, reason: REASON } },
    ]);
  });
});
