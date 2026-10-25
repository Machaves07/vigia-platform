/**
 * SCR-07 alta del nodo (IF-14 pasos 2 y 3; BR-APP-50; pendiente nº 30): declaración con
 * reemplazo y errores anclados, código de alta una sola vez con el `node_id` y todas las huellas,
 * fuera del DOM y de la caché al salir, e intentos de alta.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse, http } from 'msw';
import { Link, Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr07/enrollment';
import { fleetPort } from '@/ports/scr07Fleet';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import {
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
const NODE = node(7).node_id;
const CODE_ID = '0190a1b2-0000-7000-8000-0000000c0de1';

/** Código de 12 caracteres y huellas SHA-256 generados (semilla fija: ids de prueba estables). */
function generated(fingerprints: number) {
  const [value] = fc.sample(
    fc.record({
      code: fc.stringMatching(/^[A-HJ-NP-Z2-9]{12}$/),
      roots: fc.uniqueArray(fc.stringMatching(/^[0-9a-f]{64}$/), {
        minLength: fingerprints,
        maxLength: fingerprints,
      }),
    }),
    { seed: 437 + fingerprints, numRuns: 1 },
  );
  if (value === undefined) throw new Error('Sin muestra generada.');
  return value;
}

function codeServer(roots: readonly string[], code: string) {
  let calls = 0;
  server.use(
    http.post(`http://localhost/nodes/${NODE}/enrollment-codes`, () => {
      calls++;
      return HttpResponse.json(
        {
          code,
          code_id: CODE_ID,
          expires_at: '2026-01-02T12:00:00.000Z',
          disclosed_at: INSTANT,
          node_ca_root_sha256: roots,
        },
        { status: 201 },
      );
    }),
    http.get(`http://localhost/nodes/${NODE}/enrollment-attempts`, () =>
      HttpResponse.json({ attempts: [], next_after: null }),
    ),
  );
  return () => calls;
}

async function codePage() {
  const fixture = await fleetFixture(`/fleet/nodes/${NODE}/enrollment`, {
    plants: hierarchyPlants(),
    nodes: [hierarchyNode(7, 'declared')],
  });
  const view = render(
    <Routes>
      <Route path="/fleet/nodes/:node_id/enrollment" element={<Page />} />
      <Route path="/fleet" element={<Link to={`/fleet/nodes/${NODE}/enrollment`}>volver</Link>} />
    </Routes>,
    { wrapper: fixture.Wrapper },
  );
  return { ...fixture, ...view };
}

function cachedValues(cache: Awaited<ReturnType<typeof fleetFixture>>['cache']): unknown[] {
  return [
    ...cache.client
      .getQueryCache()
      .getAll()
      .map((query) => query.state.data),
    ...cache.client
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.state.data),
  ];
}

describe('SCR-07 alta del nodo', { timeout: 30_000 }, () => {
  it('declara las operaciones fixed con los permisos de las rutas de U-03', () => {
    const declared = Object.fromEntries(
      fleetPort.operations.map(({ name, permission, kind, status }) => [
        name,
        { permission, kind, status },
      ]),
    );
    expect({
      declare: declared.declare,
      issueCode: declared.issueCode,
      attempts: declared.attempts,
      assignZone: declared.assignZone,
      unassignZone: declared.unassignZone,
      revoke: declared.revoke,
      decommission: declared.decommission,
      publishTargetVersion: declared.publishTargetVersion,
      recaptureFraming: declared.recaptureFraming,
    }).toEqual({
      declare: { permission: 'commissioning.run', kind: 'write', status: 'fixed' },
      issueCode: { permission: 'commissioning.run', kind: 'write', status: 'fixed' },
      attempts: { permission: 'fleet.read', kind: 'read', status: 'fixed' },
      assignZone: { permission: 'commissioning.run', kind: 'write', status: 'fixed' },
      unassignZone: { permission: 'commissioning.run', kind: 'write', status: 'fixed' },
      revoke: { permission: 'fleet.manage', kind: 'write', status: 'fixed' },
      decommission: { permission: 'fleet.manage', kind: 'write', status: 'fixed' },
      publishTargetVersion: { permission: 'fleet.manage', kind: 'write', status: 'fixed' },
      recaptureFraming: { permission: 'commissioning.run', kind: 'write', status: 'fixed' },
    });
  });

  it('con dos huellas muestra las dos con el node_id y el código sale del DOM y de la caché al salir', async () => {
    const { code, roots } = generated(2);
    expect(roots).toHaveLength(2);
    const calls = codeServer(roots, code);
    const fixture = await codePage();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr07.enrollment.issue') }, WAIT),
    );
    const secret = await screen.findByRole(
      'region',
      { name: text('components_forms.secret.enrollment_code') },
      WAIT,
    );
    expect(secret.querySelector('[data-part="value"]')?.textContent).toBe(code);
    expect(secret.querySelector('[data-part="node_id"]')?.textContent).toBe(NODE);
    expect(
      [...secret.querySelectorAll('[data-part="fingerprints"] code')].map(
        (item) => item.textContent,
      ),
    ).toEqual(roots);
    expect(screen.getByText(text('scr07.enrollment.compare'))).toBeDefined();
    expect(calls()).toBe(1);
    // La caché de mutaciones solo guarda el identificador del código, nunca el código.
    await waitFor(() => {
      expect(
        fixture.cache.client
          .getMutationCache()
          .getAll()
          .map((mutation) => [mutation.state.status, mutation.state.data]),
      ).toEqual([['success', { code_id: CODE_ID }]]);
    }, WAIT);
    expect(await axe(fixture.container)).toHaveNoViolations();

    fireEvent.click(screen.getByRole('link', { name: text('scr07.title') }));
    await screen.findByRole('link', { name: 'volver' }, WAIT);
    expect(fixture.container.innerHTML.indexOf(code)).toBe(-1);
    for (const fingerprint of roots)
      expect(fixture.container.innerHTML.indexOf(fingerprint)).toBe(-1);
    expect(JSON.stringify(cachedValues(fixture.cache)).indexOf(code)).toBe(-1);

    // Al volver no se vuelve a mostrar: hay que pedir otro código.
    fireEvent.click(screen.getByRole('link', { name: 'volver' }));
    await screen.findByRole('button', { name: text('scr07.enrollment.issue') }, WAIT);
    expect(
      screen.queryByRole('region', { name: text('components_forms.secret.enrollment_code') }),
    ).toBeNull();
    expect(calls()).toBe(1);
  });

  it('con una sola huella muestra exactamente esa; «lo guardé» retira el código', async () => {
    const { code, roots } = generated(1);
    codeServer(roots, code);
    const fixture = await codePage();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr07.enrollment.issue') }, WAIT),
    );
    const secret = await screen.findByRole(
      'region',
      { name: text('components_forms.secret.enrollment_code') },
      WAIT,
    );
    expect(
      [...secret.querySelectorAll('[data-part="fingerprints"] code')].map(
        (item) => item.textContent,
      ),
    ).toEqual(roots);
    fireEvent.click(
      within(secret).getByRole('button', { name: text('components_forms.secret.acknowledge') }),
    );
    await waitFor(() => {
      expect(fixture.container.innerHTML.indexOf(code)).toBe(-1);
    }, WAIT);
  });

  it('reemitir pide otro código y muestra solo el nuevo', async () => {
    const first = generated(1);
    const second = generated(2);
    const codes = [first.code, second.code];
    let index = 0;
    server.use(
      http.post(`http://localhost/nodes/${NODE}/enrollment-codes`, () => {
        const code = codes[index] ?? '';
        index++;
        return HttpResponse.json(
          {
            code,
            code_id: `0190a1b2-0000-7000-8000-0000000c0de${String(index)}`,
            expires_at: '2026-01-02T12:00:00.000Z',
            disclosed_at: INSTANT,
            node_ca_root_sha256: second.roots,
          },
          { status: 201 },
        );
      }),
      http.get(`http://localhost/nodes/${NODE}/enrollment-attempts`, () =>
        HttpResponse.json({ attempts: [], next_after: null }),
      ),
    );
    const fixture = await codePage();
    const issue = await screen.findByRole('button', { name: text('scr07.enrollment.issue') }, WAIT);
    fireEvent.click(issue);
    await waitFor(() => {
      expect(fixture.container.querySelector('[data-part="value"]')?.textContent).toBe(first.code);
      expect(issue.hasAttribute('disabled')).toBe(false);
    }, WAIT);
    fireEvent.click(issue);
    await waitFor(() => {
      expect(fixture.container.querySelector('[data-part="value"]')?.textContent).toBe(second.code);
    }, WAIT);
    expect(fixture.container.innerHTML.indexOf(first.code)).toBe(-1);
  });

  it('fleet_node_not_declared se muestra sin ocultar la acción ni inventar un código', async () => {
    server.use(
      http.post(`http://localhost/nodes/${NODE}/enrollment-codes`, () =>
        HttpResponse.json(
          { code: 'conflict', detail_code: 'fleet_node_not_declared', correlation_id: PLANT },
          { status: 409 },
        ),
      ),
      http.get(`http://localhost/nodes/${NODE}/enrollment-attempts`, () =>
        HttpResponse.json({ attempts: [], next_after: null }),
      ),
    );
    await codePage();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr07.enrollment.issue') }, WAIT),
    );
    expect(
      await screen.findByText(label('fleet_detail_code', 'fleet_node_not_declared'), {}, WAIT),
    ).toBeDefined();
    expect(
      screen.queryByRole('region', { name: text('components_forms.secret.enrollment_code') }),
    ).toBeNull();
    expect(
      screen.getByRole('button', { name: text('scr07.enrollment.issue') }).hasAttribute('disabled'),
    ).toBe(false);
  });

  it('lista los intentos de alta con resultado etiquetado y pagina por cursor', async () => {
    const cursors: (string | null)[] = [];
    server.use(
      http.get(`http://localhost/nodes/${NODE}/enrollment-attempts`, ({ request }) => {
        const after = new URL(request.url).searchParams.get('after');
        cursors.push(after);
        return HttpResponse.json({
          attempts: [
            {
              attempt_id: '0190a1b2-0000-7000-8000-0000000a7701',
              node_id: NODE,
              result: after === null ? 'enrollment_code_expired' : 'accepted',
              hardware_fingerprint: 'HUELLA-EQUIPO-SINTETICA',
              software_version: '1.1.0',
              contract_version: '1.1.0',
              attempted_at: INSTANT,
            },
          ],
          next_after: after === null ? 'intentos-siguientes' : null,
        });
      }),
    );
    const fixture = await codePage();
    expect(
      await screen.findByRole(
        'cell',
        { name: label('enrollment_attempt_result', 'enrollment_code_expired') },
        WAIT,
      ),
    ).toBeDefined();
    expect(screen.getByRole('cell', { name: 'HUELLA-EQUIPO-SINTETICA' })).toBeDefined();
    expect(await axe(fixture.container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole('button', { name: text('scr07.load_more') }));
    await screen.findByRole('cell', { name: label('enrollment_attempt_result', 'accepted') }, WAIT);
    expect(cursors).toEqual([null, 'intentos-siguientes']);
  });
});

describe('SCR-07 declarar nodo', { timeout: 30_000 }, () => {
  async function declarePage() {
    const fixture = await fleetFixture('/fleet/enrollment', {
      plants: hierarchyPlants(),
      nodes: [hierarchyNode(3, 'revoked')],
    });
    const view = render(<Page />, { wrapper: fixture.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética' }, WAIT);
    return { ...fixture, ...view };
  }
  function fill() {
    fireEvent.change(screen.getByRole('combobox', { name: text('scr07.plant') }), {
      target: { value: PLANT },
    });
    fireEvent.change(screen.getByRole('textbox', { name: text('scr07.declare.code') }), {
      target: { value: '  NODO-NUEVO-01 ' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Zona sintética A (ZONA-SINT-A)' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Zona sintética B (ZONA-SINT-B)' }));
    fireEvent.change(screen.getByRole('combobox', { name: text('scr07.declare.replaces') }), {
      target: { value: node(3).node_id },
    });
  }

  it('envía código, zonas y replaces_node_id válidos según app.yaml y enlaza al código de alta', async () => {
    const bodies: unknown[] = [];
    const declared = node(9).node_id;
    server.use(
      http.post('http://localhost/plants/:plant_id/nodes', async ({ request, params }) => {
        expect(params.plant_id).toBe(PLANT);
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            node_id: declared,
            plant_id: PLANT,
            code: 'NODO-NUEVO-01',
            status: 'declared',
            declared_at: INSTANT,
            replaces_node_id: node(3).node_id,
            assignments: [],
          },
          { status: 201 },
        );
      }),
    );
    const fixture = await declarePage();
    fill();
    fireEvent.click(screen.getByRole('button', { name: text('scr07.declare.submit') }));
    await screen.findByText(text('scr07.declare.done'), {}, WAIT);
    expect(bodies).toEqual([
      { code: 'NODO-NUEVO-01', zone_ids: [ZONE_A, ZONE_B], replaces_node_id: node(3).node_id },
    ]);
    const validate = validators.component('NodeDeclarationBody');
    expect(validate(bodies[0])).toBe(true);
    expect(
      screen.getByRole('link', { name: text('scr07.enrollment.go') }).getAttribute('href'),
    ).toBe(`/fleet/nodes/${declared}/enrollment`);
    expect(await axe(fixture.container)).toHaveNoViolations();
  });

  it('sin código no envía nada', async () => {
    let calls = 0;
    server.use(
      http.post('http://localhost/plants/:plant_id/nodes', () => {
        calls++;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    await declarePage();
    fireEvent.change(screen.getByRole('combobox', { name: text('scr07.plant') }), {
      target: { value: PLANT },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr07.declare.submit') }));
    expect(
      screen
        .getByRole('textbox', { name: text('scr07.declare.code') })
        .getAttribute('aria-invalid'),
    ).toBe('true');
    expect(calls).toBe(0);
  });

  it.each([
    ['fleet_code_in_use', 'conflict', 409, 'code'],
    ['fleet_zone_already_served', 'conflict', 409, 'zones'],
    ['fleet_zone_in_other_plant', 'invalid_request', 400, 'zones'],
    ['fleet_replaced_node_not_found', 'invalid_request', 400, 'replaces'],
  ] as const)('ancla %s a su campo y conserva lo escrito', async (detail, code, status, field) => {
    server.use(
      http.post('http://localhost/plants/:plant_id/nodes', () =>
        HttpResponse.json({ code, detail_code: detail, correlation_id: PLANT }, { status }),
      ),
    );
    const fixture = await declarePage();
    fill();
    fireEvent.click(screen.getByRole('button', { name: text('scr07.declare.submit') }));
    const message = await screen.findByText(label('fleet_detail_code', detail), {}, WAIT);
    const anchored = message.closest('[role="alert"]');
    const target =
      field === 'code'
        ? screen.getByRole('textbox', { name: text('scr07.declare.code') })
        : field === 'zones'
          ? screen.getByRole('group', { name: text('scr07.declare.zones') })
          : screen.getByRole('combobox', { name: text('scr07.declare.replaces') });
    expect(target.getAttribute('aria-invalid')).toBe('true');
    expect(target.getAttribute('aria-describedby')).toBe(anchored?.id);
    expect(
      screen.getByRole<HTMLInputElement>('textbox', { name: text('scr07.declare.code') }).value,
    ).toBe('  NODO-NUEVO-01 ');
    expect(screen.queryByText(text('scr07.declare.done'))).toBeNull();
    expect(await axe(fixture.container)).toHaveNoViolations();
  });
});
