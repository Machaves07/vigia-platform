/** Avance de SUP-U05-38. La escritura con motivo y rangos sigue pendiente de contrato. */
import { act, fireEvent, screen, render } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr01/hierarchy';
import { LoopConfigurationView } from '@/pages/scr01/hierarchy/LoopConfiguration';
import { LOOP_FIELDS } from '@/ports/scr01Hierarchy';
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

describe('SCR-01 lectura del lazo (edición pendiente)', { timeout: 60_000 }, () => {
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
    await screen.findByText(text('scr01_hierarchy.reminder_interval_hours'), {}, WAIT);
    expect(region.querySelectorAll('dt')).toHaveLength(20);
    expect(region.querySelectorAll('input,select,textarea')).toHaveLength(0);
    expect(reads).toEqual([PLANT]);
    expect(screen.getByText(text('scr01_hierarchy.monday')).tagName).toBe('LI');
    expect(screen.getByText('2026-03-23').tagName).toBe('LI');
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
    await screen.findByText('2', { selector: 'dd' }, WAIT);
    fireEvent.click(buttons[1] ?? screen.getByRole('main'));
    await screen.findByText('29', { selector: 'dd' }, WAIT);
    expect(screen.queryByText('2', { selector: 'dd' })).toBeNull();
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

  it('axe sin violaciones serias en la configuración de lectura', async () => {
    const { container } = await ready();
    fireEvent.click(
      screen.getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }),
    );
    await screen.findByText(text('scr01_hierarchy.reminder_interval_hours'), {}, WAIT);
    const result = await axe(container);
    expect(
      result.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
    ).toEqual([]);
  });
});
