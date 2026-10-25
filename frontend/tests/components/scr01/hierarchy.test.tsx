import { act, fireEvent, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr01/hierarchy';
import { hierarchyPort } from '@/ports/scr01Hierarchy';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { assertNoPersonData } from '../../support/viewProperties';
import views from '../../views/scr01_hierarchy';
import {
  apiError,
  configuration,
  hierarchy,
  OTHER_PLANT,
  PLANT,
  renderAdmin,
  settings,
  WAIT,
  ZONE,
} from './hierarchySupport';

async function ready() {
  const view = await renderAdmin(<Page />);
  await screen.findByRole('heading', { name: 'PLANTA-SINT — Planta sintética' }, WAIT);
  return view;
}

describe('SCR-01 jerarquía', { timeout: 60_000 }, () => {
  it('RV-03 rechaza la filtración de campos inyectados en el componente de producción', () => {
    assertNoPersonData(views.hierarchy);
  });
  it('declara las siete operaciones fixed con sus permisos reales', () => {
    expect(
      hierarchyPort.operations.map(({ name, status, permission, kind }) => ({
        name,
        status,
        permission,
        kind,
      })),
    ).toEqual([
      { name: 'hierarchy', status: 'fixed', permission: 'session', kind: 'read' },
      { name: 'createPlant', status: 'fixed', permission: 'hierarchy.manage', kind: 'write' },
      { name: 'createZone', status: 'fixed', permission: 'hierarchy.manage', kind: 'write' },
      { name: 'settings', status: 'fixed', permission: 'organization.settings', kind: 'read' },
      { name: 'saveSettings', status: 'fixed', permission: 'organization.settings', kind: 'write' },
      { name: 'configuration', status: 'fixed', permission: 'hierarchy.read', kind: 'read' },
      {
        name: 'saveConfiguration',
        status: 'fixed',
        permission: 'organization.settings',
        kind: 'write',
      },
    ]);
  });

  it('muestra país, región como texto, zona horaria, zonas y nodo; no ofrece selectores de región u organización', async () => {
    const view = await ready();
    expect(view.container.querySelector('[data-part="data-region"]')?.textContent).toBe('co');
    expect(screen.getByText('America/Bogota').tagName).toBe('DD');
    expect(screen.getByText('Z-SINT — Zona sintética').tagName).toBe('SPAN');
    expect(screen.getByText(`${text('scr01_hierarchy.current_node')}: NODO-SINT`).tagName).toBe(
      'P',
    );
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
    expect(
      view.container.querySelectorAll(
        'input[name="data_region"],input[name="organization_id"],select,textarea',
      ),
    ).toHaveLength(0);
    expect(view.container.querySelector('[data-part="data-region"] input')).toBeNull();
  });

  it('ancla la validación espejo de zona antes de enviar y luego crea en la planta correcta e invalida jerarquía', async () => {
    const view = await ready();
    const calls: { plant: string; body: unknown }[] = [];
    let reads = 0;
    server.use(
      http.post('http://localhost/plants/:plantId/zones', async ({ params, request }) => {
        calls.push({ plant: String(params.plantId), body: await request.json() });
        return HttpResponse.json(
          {
            zone_id: ZONE,
            plant_id: PLANT,
            code: 'Z-NUEVA',
            name: 'Zona nueva sintética',
            node_id: null,
          },
          { status: 201 },
        );
      }),
      http.get('http://localhost/hierarchy', () => {
        reads += 1;
        return HttpResponse.json(hierarchy());
      }),
    );
    const form = screen.getByRole('form', { name: text('scr01_hierarchy.create_zone') });
    fireEvent.submit(form);
    await act(async () => {
      await Promise.resolve();
    });
    const code = within(form).getByRole('textbox', { name: text('scr01_hierarchy.code') });
    expect(code.getAttribute('aria-invalid')).toBe('true');
    expect(document.getElementById(code.getAttribute('aria-describedby') ?? '')?.textContent).toBe(
      text('validation.too_short'),
    );
    expect(calls).toHaveLength(0);
    fireEvent.change(code, { target: { value: 'Z-NUEVA' } });
    fireEvent.change(within(form).getByRole('textbox', { name: text('scr01_hierarchy.name') }), {
      target: { value: 'Zona nueva sintética' },
    });
    fireEvent.submit(form);
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(calls).toEqual([
      { plant: PLANT, body: { code: 'Z-NUEVA', name: 'Zona nueva sintética' } },
    ]);
    expect(reads).toBe(1);
    expect(
      view.cache.client
        .getQueryCache()
        .getAll()
        .every((query) => query.queryKey[0] === 'scr01'),
    ).toBe(true);
  });

  it('conserva el borrador tras rechazo del servidor y no reintenta la escritura', async () => {
    const { clock } = await ready();
    let writes = 0;
    server.use(
      http.post('http://localhost/plants/:plantId/zones', () => {
        writes += 1;
        return apiError('conflict', 409);
      }),
    );
    const form = screen.getByRole('form', { name: text('scr01_hierarchy.create_zone') });
    fireEvent.change(within(form).getByRole('textbox', { name: text('scr01_hierarchy.code') }), {
      target: { value: 'Z-NUEVA' },
    });
    fireEvent.change(within(form).getByRole('textbox', { name: text('scr01_hierarchy.name') }), {
      target: { value: 'Zona nueva sintética' },
    });
    fireEvent.submit(screen.getByRole('form', { name: text('scr01_hierarchy.create_zone') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(
      within(form).getByRole<HTMLInputElement>('textbox', { name: text('scr01_hierarchy.code') })
        .value,
    ).toBe('Z-NUEVA');
    await act(async () => {
      clock.advance(60_000);
      await Promise.resolve();
    });
    expect(writes).toBe(1);
  });

  it('presenta empty sin inventar plantas y el estado uniforme para not_found', async () => {
    const { cache } = await ready();
    server.use(
      http.get('http://localhost/hierarchy', () =>
        HttpResponse.json({ ...hierarchy(), plants: [], nodes: [] }),
      ),
    );
    await act(async () => {
      await cache.client.refetchQueries();
    });
    await screen.findByText(text('scr01_hierarchy.empty'), {}, WAIT);
    server.use(http.get('http://localhost/hierarchy', () => apiError('not_found', 404)));
    await act(async () => {
      await cache.client.refetchQueries();
    });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
  });

  it('axe sin violaciones serias en la lectura y alta de zonas', async () => {
    const { container } = await ready();
    const result = await axe(container);
    expect(
      result.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
    ).toEqual([]);
  });

  it('crea la primera planta con la región del despliegue como texto, sin campos de región u organización', async () => {
    const { cache, container } = await ready();
    server.use(
      http.get('http://localhost/hierarchy', () =>
        HttpResponse.json({ ...hierarchy(), plants: [], nodes: [] }),
      ),
    );
    await act(async () => {
      await cache.client.refetchQueries();
    });
    const calls: unknown[] = [];
    server.use(
      http.post('http://localhost/plants', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(hierarchy().plants[0], { status: 201 });
      }),
    );
    const form = screen.getByRole('form', { name: text('scr01_hierarchy.create_plant') });
    const fields = {
      code: 'P-NUEVA',
      name: 'Planta nueva sintética',
      country: 'MX',
      timezone: 'America/Mexico_City',
    };
    for (const [field, value] of Object.entries(fields))
      fireEvent.change(
        within(form).getByRole('textbox', { name: text(`scr01_hierarchy.${field}`) }),
        { target: { value } },
      );
    fireEvent.submit(form);
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(calls).toEqual([{ ...fields, data_region: 'co' }]);
    expect(container.querySelector('[data-part="plant-region"]')?.textContent).toBe('co');
    expect(
      form.querySelector('input[name="data_region"],input[name="organization_id"],select'),
    ).toBeNull();
    expect(within(form).getAllByRole('textbox')).toHaveLength(4);
  });

  it.each([null, '', 'x'.repeat(33)])(
    'sin región de despliegue válida (%j), no inventa una ni envía el alta',
    async (dataRegion) => {
      await renderAdmin(<Page />, { dataRegion });
      await screen.findByRole('heading', { name: 'PLANTA-SINT — Planta sintética' }, WAIT);
      let calls = 0;
      server.use(
        http.post('http://localhost/plants', () => {
          calls += 1;
          return HttpResponse.json({}, { status: 201 });
        }),
      );
      const form = screen.getByRole('form', { name: text('scr01_hierarchy.create_plant') });
      expect(
        within(form).getByRole('button', { name: text('scr01_hierarchy.create_plant') }),
      ).toHaveProperty('disabled', true);
      fireEvent.submit(form);
      await act(async () => {
        await Promise.resolve();
      });
      expect(calls).toBe(0);
      expect(form.querySelector('[data-part="plant-region"]')?.textContent).toBe(
        text('scr01_hierarchy.region_unconfigured'),
      );
    },
  );

  it('ancla país y zona horaria inválidos antes de enviar una planta', async () => {
    await ready();
    const form = screen.getByRole('form', { name: text('scr01_hierarchy.create_plant') });
    fireEvent.submit(form);
    for (const field of ['country', 'timezone']) {
      const input = within(form).getByRole('textbox', { name: text(`scr01_hierarchy.${field}`) });
      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(
        document.getElementById(input.getAttribute('aria-describedby') ?? '')?.textContent,
      ).toBe(text('validation.too_short'));
    }
  });

  it('recorre rutas reales: crea planta, crea zona, cambia lazo con motivo y guarda ajustes de organización', async () => {
    await renderAdmin(null, { routes: true });
    await screen.findByRole('heading', { name: text('scr01_hierarchy.title') }, WAIT);
    let current = hierarchy();
    let loop = configuration(OTHER_PLANT);
    const writes: { operation: string; plant?: string; body: unknown }[] = [];
    server.use(
      http.get('http://localhost/hierarchy', () => HttpResponse.json(current)),
      http.post('http://localhost/plants', async ({ request }) => {
        const body = (await request.json()) as {
          code: string;
          name: string;
          country: string;
          timezone: string;
          data_region: string;
        };
        const plant = { ...body, plant_id: OTHER_PLANT, status: 'active', zones: [] };
        current = { ...current, plants: [...current.plants, plant] };
        writes.push({ operation: 'plant', body });
        return HttpResponse.json(plant, { status: 201 });
      }),
      http.post('http://localhost/plants/:plantId/zones', async ({ request, params }) => {
        const body = (await request.json()) as { code: string; name: string };
        const zone = { ...body, zone_id: ZONE, plant_id: String(params.plantId), node_id: null };
        current = {
          ...current,
          plants: current.plants.map((plant) =>
            plant.plant_id === params.plantId ? { ...plant, zones: [zone] } : plant,
          ),
        };
        writes.push({ operation: 'zone', plant: String(params.plantId), body });
        return HttpResponse.json(zone, { status: 201 });
      }),
      http.get('http://localhost/loop/plants/:plantId/configuration', () =>
        HttpResponse.json(loop),
      ),
      http.put(
        'http://localhost/loop/plants/:plantId/configuration',
        async ({ request, params }) => {
          const body = await request.json();
          writes.push({ operation: 'loop', plant: String(params.plantId), body });
          loop = { ...loop, classification_deadline_business_days: 4, version: 2 };
          return HttpResponse.json(loop);
        },
      ),
      http.patch('http://localhost/organization/settings', async ({ request }) => {
        const body = await request.json();
        writes.push({ operation: 'settings', body });
        return HttpResponse.json(settings());
      }),
    );
    const plantForm = screen.getByRole('form', { name: text('scr01_hierarchy.create_plant') });
    const plantBody = {
      code: 'P-RECORRIDO',
      name: 'Planta del recorrido',
      country: 'CO',
      timezone: 'America/Bogota',
    };
    for (const [field, value] of Object.entries(plantBody))
      fireEvent.change(
        within(plantForm).getByRole('textbox', { name: text(`scr01_hierarchy.${field}`) }),
        { target: { value } },
      );
    fireEvent.submit(plantForm);
    const heading = await screen.findByRole(
      'heading',
      { name: 'P-RECORRIDO — Planta del recorrido' },
      WAIT,
    );
    const plantRow = heading.closest('li');
    if (plantRow === null) throw new Error('Falta fila de la planta creada');
    const zoneForm = within(plantRow).getByRole('form', {
      name: text('scr01_hierarchy.create_zone'),
    });
    fireEvent.change(
      within(zoneForm).getByRole('textbox', { name: text('scr01_hierarchy.code') }),
      { target: { value: 'Z-RECORRIDO' } },
    );
    fireEvent.change(
      within(zoneForm).getByRole('textbox', { name: text('scr01_hierarchy.name') }),
      { target: { value: 'Zona del recorrido' } },
    );
    fireEvent.submit(zoneForm);
    await within(plantRow).findByText('Z-RECORRIDO — Zona del recorrido', {}, WAIT);
    fireEvent.click(
      within(plantRow).getByRole('button', { name: text('scr01_hierarchy.loop_configuration') }),
    );
    const form = await within(plantRow).findByRole(
      'form',
      { name: text('scr01_hierarchy.edit_loop') },
      WAIT,
    );
    fireEvent.change(
      within(form).getByRole('spinbutton', {
        name: text('scr01_hierarchy.classification_deadline_business_days'),
      }),
      { target: { value: '4' } },
    );
    const reason = 'Cambio sintético durante el recorrido de administración.';
    fireEvent.change(
      within(form).getByRole('textbox', {
        name: `${text('scr01_hierarchy.reason')} (${text('components_forms.reason.required')})`,
      }),
      { target: { value: reason } },
    );
    fireEvent.submit(form);
    await within(form).findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    fireEvent.click(screen.getByRole('link', { name: text('scr01_hierarchy.organization') }));
    const organizationForm = await screen.findByRole(
      'form',
      { name: text('scr01_hierarchy.concession_settings') },
      WAIT,
    );
    fireEvent.change(
      within(organizationForm).getByRole('spinbutton', {
        name: text('scr01_hierarchy.concession_max_days'),
      }),
      { target: { value: '14' } },
    );
    fireEvent.submit(organizationForm);
    await within(organizationForm).findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(writes).toEqual([
      { operation: 'plant', body: { ...plantBody, data_region: 'co' } },
      {
        operation: 'zone',
        plant: OTHER_PLANT,
        body: { code: 'Z-RECORRIDO', name: 'Zona del recorrido' },
      },
      {
        operation: 'loop',
        plant: OTHER_PLANT,
        body: { classification_deadline_business_days: 4, reason },
      },
      { operation: 'settings', body: { concession_max_days: 14, concession_default_days: 7 } },
    ]);
  });
});
