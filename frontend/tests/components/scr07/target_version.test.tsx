/**
 * SCR-07 versión objetivo (IF-14 paso 5; A-61): `POST /fleet/target-versions` lleva `plant_id` en
 * el cuerpo y `group = "plant"` o la lista de nodos; MSW valida cada cuerpo con Ajv contra
 * `TargetVersionBody` de `app.yaml`. `fleet_version_outside_contract_window` se ancla a la versión.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr07/target_version';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import { fleetFixture, hierarchyNode, hierarchyPlants, INSTANT, node, PLANT } from './support';

const WAIT = { timeout: 10_000 };
const validateBody = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
).component('TargetVersionBody');

/** Manejador que rechaza con 422 todo cuerpo que no valide contra el esquema de `app.yaml`. */
function publicationServer() {
  const accepted: Record<string, unknown>[] = [];
  const rejected: unknown[] = [];
  server.use(
    http.post('http://localhost/fleet/target-versions', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      if (!validateBody(body)) {
        rejected.push(validateBody.errors);
        return HttpResponse.json(
          { code: 'invalid_request', correlation_id: PLANT },
          { status: 422 },
        );
      }
      accepted.push(body);
      return HttpResponse.json(
        {
          publication_id: '0190a1b2-0000-7000-8000-0000000b0001',
          plant_id: PLANT,
          target_version: body.target_version,
          node_ids: Array.isArray(body.node_ids)
            ? body.node_ids
            : [node(1).node_id, node(2).node_id],
          maintenance_window: body.maintenance_window,
          published_by: '0190a1b2-0000-7000-8000-0000000b0002',
          published_at: INSTANT,
          ledger_record_id: '0190a1b2-0000-7000-8000-0000000b0003',
        },
        { status: 201 },
      );
    }),
  );
  return { accepted, rejected };
}

async function page() {
  const fixture = await fleetFixture('/fleet/target-versions', {
    plants: hierarchyPlants(),
    nodes: [hierarchyNode(1), hierarchyNode(2), hierarchyNode(3)],
  });
  const view = render(<Page />, { wrapper: fixture.Wrapper });
  await screen.findByRole('option', { name: 'Planta sintética' }, WAIT);
  return { ...fixture, ...view };
}

function fill(version = '1.2.0') {
  fireEvent.change(screen.getByRole('combobox', { name: text('scr07.plant') }), {
    target: { value: PLANT },
  });
  fireEvent.change(screen.getByRole('textbox', { name: text('scr07.target') }), {
    target: { value: version },
  });
  fireEvent.change(screen.getByLabelText(text('scr07.target_version.from')), {
    target: { value: '2026-02-01T08:00' },
  });
  fireEvent.change(screen.getByLabelText(text('scr07.target_version.to')), {
    target: { value: '2026-02-01T10:30' },
  });
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: text('scr07.target_version.submit') }));
}

describe('SCR-07 versión objetivo', { timeout: 30_000 }, () => {
  it('por planta envía plant_id y group = "plant" validados con Ajv; axe sin violaciones', async () => {
    const { accepted, rejected } = publicationServer();
    const fixture = await page();
    fill();
    submit();
    const receipt = await screen.findByRole(
      'region',
      { name: text('scr07.target_version.published') },
      WAIT,
    );
    expect(rejected).toEqual([]);
    expect(accepted).toHaveLength(1);
    expect(accepted[0]?.plant_id).toBe(PLANT);
    expect(accepted[0]?.group).toBe('plant');
    // America/Bogota es UTC−5: la ventana se envía en UTC con milisegundos.
    expect(accepted[0]).toEqual({
      plant_id: PLANT,
      group: 'plant',
      target_version: '1.2.0',
      maintenance_window: { from: '2026-02-01T13:00:00.000Z', to: '2026-02-01T15:30:00.000Z' },
    });
    expect(within(receipt).getByText('2')).toBeDefined();
    expect(
      within(receipt)
        .getByRole('link', { name: text('scr07.title') })
        .getAttribute('href'),
    ).toBe('/fleet');
    expect(await axe(fixture.container)).toHaveNoViolations();
  });

  it('por nodos elegidos envía node_ids de la planta sin group', async () => {
    const { accepted, rejected } = publicationServer();
    await page();
    fill();
    fireEvent.click(screen.getByRole('radio', { name: text('scr07.target_version.some_nodes') }));
    submit();
    expect(
      await screen.findByText(text('scr07.target_version.nodes_required'), {}, WAIT),
    ).toBeDefined();
    expect(accepted).toEqual([]);
    fireEvent.click(screen.getByRole('checkbox', { name: node(2).code }));
    submit();
    await screen.findByRole('region', { name: text('scr07.target_version.published') }, WAIT);
    expect(rejected).toEqual([]);
    expect(accepted).toEqual([
      {
        plant_id: PLANT,
        node_ids: [node(2).node_id],
        target_version: '1.2.0',
        maintenance_window: { from: '2026-02-01T13:00:00.000Z', to: '2026-02-01T15:30:00.000Z' },
      },
    ]);
  });

  it.each([
    ['2026-02-01T10:30', '2026-02-01T08:00'],
    ['2026-02-01T08:00', '2026-02-01T08:00'],
    ['', '2026-02-01T08:00'],
  ])('una ventana %s → %s no se envía', async (from, to) => {
    const { accepted } = publicationServer();
    await page();
    fill();
    fireEvent.change(screen.getByLabelText(text('scr07.target_version.from')), {
      target: { value: from },
    });
    fireEvent.change(screen.getByLabelText(text('scr07.target_version.to')), {
      target: { value: to },
    });
    submit();
    expect(screen.getByText(text('scr07.target_version.window_invalid'))).toBeDefined();
    expect(accepted).toEqual([]);
  });

  it('fleet_version_outside_contract_window se ancla a la versión y conserva lo escrito', async () => {
    server.use(
      http.post('http://localhost/fleet/target-versions', () =>
        HttpResponse.json(
          {
            code: 'invalid_request',
            detail_code: 'fleet_version_outside_contract_window',
            correlation_id: PLANT,
          },
          { status: 400 },
        ),
      ),
    );
    const fixture = await page();
    fill('9.0.0');
    submit();
    const message = await screen.findByText(
      label('fleet_detail_code', 'fleet_version_outside_contract_window'),
      {},
      WAIT,
    );
    const version = screen.getByRole<HTMLInputElement>('textbox', { name: text('scr07.target') });
    expect(version.getAttribute('aria-invalid')).toBe('true');
    expect(version.getAttribute('aria-describedby')).toBe(message.closest('[role="alert"]')?.id);
    expect(version.value).toBe('9.0.0');
    expect(
      screen.queryByRole('region', { name: text('scr07.target_version.published') }),
    ).toBeNull();
    expect(await axe(fixture.container)).toHaveNoViolations();
  });
});
