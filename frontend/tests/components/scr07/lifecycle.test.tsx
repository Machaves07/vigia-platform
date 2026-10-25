/**
 * SCR-07 ciclo de vida del nodo (IF-14 paso 4; A-55): asignar y retirar zona con motivo, revocar
 * y dar de baja con motivo y confirmación explícita (la baja solo tras `revoked` del servidor) y
 * recaptura del encuadre.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr07/lifecycle';
import { Route, Routes } from 'react-router';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import {
  CAMERA,
  fleetFixture,
  hierarchyNode,
  hierarchyPlants,
  INSTANT,
  node,
  PLANT,
  ZONE_A,
  ZONE_B,
} from './support';

const WAIT = { timeout: 10_000 };
const validators = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
);
const NODE = node(5).node_id;
const REASON = 'Motivo sintético de la operación';

function detail(status: string) {
  return {
    node: {
      ...node(5),
      status,
      zones: [ZONE_A],
      cameras: [
        {
          camera_id: CAMERA,
          code: 'CAM-SINT-1',
          connected: true,
          measured_fps: 10,
          declared_min_fps: 5,
          observability_state: 'observable',
        },
      ],
    },
    heartbeat_history: [],
    next_history_after: null,
  };
}

async function lifecycle(status: string) {
  server.use(
    http.get(`http://localhost/fleet/nodes/${NODE}`, () => HttpResponse.json(detail(status))),
  );
  const fixture = await fleetFixture(`/fleet/nodes/${NODE}/lifecycle`, {
    plants: hierarchyPlants(),
    nodes: [hierarchyNode(5, status)],
  });
  const view = render(
    <Routes>
      <Route path="/fleet/nodes/:node_id/lifecycle" element={<Page />} />
    </Routes>,
    { wrapper: fixture.Wrapper },
  );
  await screen.findByRole('region', { name: text('scr07.lifecycle.revoke') }, WAIT);
  return { ...fixture, ...view };
}

/** Escrituras iniciadas: TanStack añade la mutación a su caché de forma síncrona al enviar. */
function writes(fixture: Awaited<ReturnType<typeof fleetFixture>>): number {
  return fixture.cache.client.getMutationCache().getAll().length;
}

function form(name: string) {
  return screen.getByRole('form', { name });
}

function writeReason(scope: HTMLElement, value = REASON) {
  fireEvent.change(within(scope).getByRole('textbox', { name: /^Motivo/u }), {
    target: { value },
  });
}

function revocationServer(status: string) {
  const bodies: unknown[] = [];
  server.use(
    http.post(`http://localhost/nodes/${NODE}/revocation`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json({ node_id: NODE, status, revoked_at: INSTANT });
    }),
  );
  return bodies;
}

function decommissionServer() {
  const bodies: unknown[] = [];
  server.use(
    http.post(`http://localhost/nodes/${NODE}/decommission`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json({
        node_id: NODE,
        revoked_at: INSTANT,
        decommissioned_at: '2026-01-01T13:00:00.000Z',
      });
    }),
  );
  return bodies;
}

describe('SCR-07 revocar y dar de baja', { timeout: 30_000 }, () => {
  it('sin revocar no ofrece la baja; sin confirmación no revoca; tras revoked del servidor la ofrece y exige confirmación', async () => {
    const revocations = revocationServer('revoked');
    const decommissions = decommissionServer();
    const fixture = await lifecycle('enrolled');
    expect(screen.queryByRole('region', { name: text('scr07.lifecycle.decommission') })).toBeNull();

    const revoke = form(text('scr07.lifecycle.revoke'));
    writeReason(revoke);
    fireEvent.click(
      within(revoke).getByRole('button', { name: text('scr07.lifecycle.revoke_submit') }),
    );
    expect(within(revoke).getByRole('alert').getAttribute('data-text-key')).toBe(
      'scr07.lifecycle.confirm_required',
    );
    expect(
      within(revoke)
        .getByRole('checkbox', { name: text('scr07.lifecycle.revoke_confirm') })
        .getAttribute('aria-invalid'),
    ).toBe('true');
    expect(writes(fixture)).toBe(0);
    expect(revocations).toEqual([]);

    fireEvent.click(
      within(revoke).getByRole('checkbox', { name: text('scr07.lifecycle.revoke_confirm') }),
    );
    fireEvent.click(
      within(revoke).getByRole('button', { name: text('scr07.lifecycle.revoke_submit') }),
    );
    const offered = await screen.findByRole(
      'region',
      { name: text('scr07.lifecycle.decommission') },
      WAIT,
    );
    expect(revocations).toEqual([{ reason_es: REASON }]);
    expect(validators.component('NodeReasonBody')(revocations[0])).toBe(true);

    const decommission = within(offered).getByRole('form', {
      name: text('scr07.lifecycle.decommission'),
    });
    writeReason(decommission);
    fireEvent.click(
      within(decommission).getByRole('button', {
        name: text('scr07.lifecycle.decommission_submit'),
      }),
    );
    expect(within(decommission).getByRole('alert').getAttribute('data-text-key')).toBe(
      'scr07.lifecycle.confirm_required',
    );
    expect(writes(fixture)).toBe(1);
    expect(decommissions).toEqual([]);

    fireEvent.click(
      within(decommission).getByRole('checkbox', {
        name: text('scr07.lifecycle.decommission_confirm'),
      }),
    );
    fireEvent.click(
      within(decommission).getByRole('button', {
        name: text('scr07.lifecycle.decommission_submit'),
      }),
    );
    await waitFor(() => {
      expect(decommissions).toEqual([{ reason_es: REASON }]);
    }, WAIT);
    await within(offered).findByRole('status', {}, WAIT);
    expect(offered.querySelector('time[datetime="2026-01-01T13:00:00.000Z"]')).not.toBeNull();
    expect(await axe(fixture.container)).toHaveNoViolations();
  });

  it('con el nodo revoked según el detalle del servidor ofrece la baja desde el principio', async () => {
    await lifecycle('revoked');
    expect(
      screen.getByRole('region', { name: text('scr07.lifecycle.decommission') }),
    ).toBeDefined();
  });

  it('si la respuesta de la revocación no dice revoked, la baja no se ofrece', async () => {
    const revocations = revocationServer('re_enrollment_pending');
    await lifecycle('enrolled');
    const revoke = form(text('scr07.lifecycle.revoke'));
    writeReason(revoke);
    fireEvent.click(
      within(revoke).getByRole('checkbox', { name: text('scr07.lifecycle.revoke_confirm') }),
    );
    fireEvent.click(
      within(revoke).getByRole('button', { name: text('scr07.lifecycle.revoke_submit') }),
    );
    await waitFor(() => {
      expect(revocations).toHaveLength(1);
    }, WAIT);
    await waitFor(() => {
      expect(
        within(revoke)
          .getByRole('button', { name: text('scr07.lifecycle.revoke_submit') })
          .hasAttribute('disabled'),
      ).toBe(false);
    }, WAIT);
    expect(screen.queryByRole('region', { name: text('scr07.lifecycle.decommission') })).toBeNull();
  });

  it('un motivo por debajo de 10 caracteres no se envía aunque esté confirmado', async () => {
    const revocations = revocationServer('revoked');
    const fixture = await lifecycle('enrolled');
    const revoke = form(text('scr07.lifecycle.revoke'));
    writeReason(revoke, 'corto');
    fireEvent.click(
      within(revoke).getByRole('checkbox', { name: text('scr07.lifecycle.revoke_confirm') }),
    );
    fireEvent.click(
      within(revoke).getByRole('button', { name: text('scr07.lifecycle.revoke_submit') }),
    );
    expect(writes(fixture)).toBe(0);
    expect(revocations).toEqual([]);
    expect(
      within(revoke)
        .getByRole('textbox', { name: /^Motivo/u })
        .getAttribute('aria-invalid'),
    ).toBe('true');
  });

  it('fleet_node_not_revoked del servidor se muestra en la baja sin ocultarla', async () => {
    server.use(
      http.post(`http://localhost/nodes/${NODE}/decommission`, () =>
        HttpResponse.json(
          { code: 'conflict', detail_code: 'fleet_node_not_revoked', correlation_id: PLANT },
          { status: 409 },
        ),
      ),
    );
    await lifecycle('revoked');
    const decommission = form(text('scr07.lifecycle.decommission'));
    writeReason(decommission);
    fireEvent.click(
      within(decommission).getByRole('checkbox', {
        name: text('scr07.lifecycle.decommission_confirm'),
      }),
    );
    fireEvent.click(
      within(decommission).getByRole('button', {
        name: text('scr07.lifecycle.decommission_submit'),
      }),
    );
    expect(
      await within(decommission).findByText(
        label('fleet_detail_code', 'fleet_node_not_revoked'),
        {},
        WAIT,
      ),
    ).toBeDefined();
    expect(
      screen.getByRole('region', { name: text('scr07.lifecycle.decommission') }),
    ).toBeDefined();
  });
});

describe('SCR-07 zonas del nodo', { timeout: 30_000 }, () => {
  it('asigna una zona de la planta y retira otra con motivo', async () => {
    const assigned: unknown[] = [];
    const removed: { zone: unknown; body: unknown }[] = [];
    server.use(
      http.post(`http://localhost/nodes/${NODE}/zones`, async ({ request }) => {
        assigned.push(await request.json());
        return HttpResponse.json(
          {
            assignment_id: '0190a1b2-0000-7000-8000-0000000a5501',
            node_id: NODE,
            zone_id: ZONE_B,
            assigned_at: INSTANT,
            unassigned_at: null,
          },
          { status: 201 },
        );
      }),
      http.post(
        `http://localhost/nodes/${NODE}/zones/:zone_id/unassignment`,
        async ({ request, params }) => {
          removed.push({ zone: params.zone_id, body: await request.json() });
          return HttpResponse.json({
            assignment_id: '0190a1b2-0000-7000-8000-0000000a5502',
            node_id: NODE,
            zone_id: ZONE_A,
            assigned_at: INSTANT,
            unassigned_at: INSTANT,
          });
        },
      ),
    );
    const fixture = await lifecycle('enrolled');
    const assign = form(text('scr07.lifecycle.assign'));
    // Solo se ofrecen las zonas de la planta que el nodo aún no atiende.
    expect(
      within(assign)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['', ZONE_B]);
    fireEvent.change(within(assign).getByRole('combobox', { name: text('scr07.zone') }), {
      target: { value: ZONE_B },
    });
    fireEvent.click(
      within(assign).getByRole('button', { name: text('scr07.lifecycle.assign_submit') }),
    );
    await within(assign).findByText(text('scr07.lifecycle.assigned'), {}, WAIT);
    expect(assigned).toEqual([{ zone_id: ZONE_B }]);

    const unassign = form(text('scr07.lifecycle.unassign'));
    fireEvent.change(within(unassign).getByRole('combobox', { name: text('scr07.zone') }), {
      target: { value: ZONE_A },
    });
    fireEvent.click(
      within(unassign).getByRole('button', { name: text('scr07.lifecycle.unassign_submit') }),
    );
    expect(writes(fixture)).toBe(1);
    expect(removed).toEqual([]);
    writeReason(unassign);
    fireEvent.click(
      within(unassign).getByRole('button', { name: text('scr07.lifecycle.unassign_submit') }),
    );
    await within(unassign).findByText(text('scr07.lifecycle.unassigned'), {}, WAIT);
    expect(removed).toEqual([{ zone: ZONE_A, body: { reason_es: REASON } }]);
    expect(await axe(fixture.container)).toHaveNoViolations();
  });

  it.each([
    ['fleet_zone_already_served', 'conflict', 409],
    ['fleet_zone_in_other_plant', 'invalid_request', 400],
  ] as const)('ancla %s a la zona', async (detail, code, status) => {
    server.use(
      http.post(`http://localhost/nodes/${NODE}/zones`, () =>
        HttpResponse.json({ code, detail_code: detail, correlation_id: PLANT }, { status }),
      ),
    );
    await lifecycle('enrolled');
    const assign = form(text('scr07.lifecycle.assign'));
    fireEvent.change(within(assign).getByRole('combobox', { name: text('scr07.zone') }), {
      target: { value: ZONE_B },
    });
    fireEvent.click(
      within(assign).getByRole('button', { name: text('scr07.lifecycle.assign_submit') }),
    );
    const message = await within(assign).findByText(label('fleet_detail_code', detail), {}, WAIT);
    const zone = within(assign).getByRole('combobox', { name: text('scr07.zone') });
    expect(zone.getAttribute('aria-invalid')).toBe('true');
    expect(zone.getAttribute('aria-describedby')).toBe(message.closest('[role="alert"]')?.id);
  });
});

describe('SCR-07 recaptura del encuadre (A-55)', { timeout: 30_000 }, () => {
  it('envía cámara, captura en hora de la planta y motivo; muestra la causa framing_recaptured', async () => {
    const bodies: { zone: unknown; body: unknown }[] = [];
    server.use(
      http.post(
        'http://localhost/zones/:zone_id/framing-recaptures',
        async ({ request, params }) => {
          bodies.push({ zone: params.zone_id, body: await request.json() });
          return HttpResponse.json(
            {
              zone_id: ZONE_A,
              state: 'pending',
              marked_at: INSTANT,
              cause: 'framing_recaptured',
              catalog_version: 3,
              model_version: null,
              affected_row_ids: 'all',
            },
            { status: 201 },
          );
        },
      ),
    );
    const fixture = await lifecycle('enrolled');
    const recapture = form(text('scr07.framing.title'));
    fireEvent.change(within(recapture).getByRole('combobox', { name: text('scr07.zone') }), {
      target: { value: ZONE_A },
    });
    fireEvent.change(within(recapture).getByRole('combobox', { name: text('scr07.camera') }), {
      target: { value: CAMERA },
    });
    fireEvent.change(within(recapture).getByLabelText(text('scr07.framing.captured_at')), {
      target: { value: '2026-01-01T06:30' },
    });
    writeReason(recapture);
    fireEvent.click(within(recapture).getByRole('button', { name: text('scr07.framing.submit') }));
    const status = await screen.findByText(
      `${text('scr07.framing.marked')}: ${label('regression_cause', 'framing_recaptured')}`,
      {},
      WAIT,
    );
    expect(status.getAttribute('role')).toBe('status');
    // America/Bogota es UTC−5: 06:30 en la planta son las 11:30 UTC.
    expect(bodies).toEqual([
      {
        zone: ZONE_A,
        body: {
          camera_id: CAMERA,
          captured_at: '2026-01-01T11:30:00.000Z',
          reason_es: REASON,
        },
      },
    ]);
    expect(validators.component('FramingRecaptureBody')(bodies[0]?.body)).toBe(true);
    expect(await axe(fixture.container)).toHaveNoViolations();
  });
});
