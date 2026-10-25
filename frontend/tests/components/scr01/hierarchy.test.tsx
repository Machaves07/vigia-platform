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
import { apiError, hierarchy, PLANT, renderAdmin, WAIT, ZONE } from './hierarchySupport';

async function ready() {
  const view = await renderAdmin(<Page />);
  await screen.findByRole('heading', { name: 'PLANTA-SINT — Planta sintética' }, WAIT);
  return view;
}

describe('SCR-01 jerarquía (avance parcial sin alta de plantas)', { timeout: 60_000 }, () => {
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
    fireEvent.change(screen.getByRole('textbox', { name: text('scr01_hierarchy.code') }), {
      target: { value: 'Z-NUEVA' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: text('scr01_hierarchy.name') }), {
      target: { value: 'Zona nueva sintética' },
    });
    fireEvent.submit(screen.getByRole('form', { name: text('scr01_hierarchy.create_zone') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(
      screen.getByRole<HTMLInputElement>('textbox', { name: text('scr01_hierarchy.code') }).value,
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
});
