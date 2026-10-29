/**
 * Inicio del administrador (VIG-365, «Administrador · Inicio»): «Pendiente de su parte» con sus
 * lecturas (invitaciones sin aceptar, zonas sin acta y nodos sin comunicación), cada pendiente con
 * su botón, las áreas de configuración de su menú y la flota debajo, con una sola actualización.
 * Cada pendiente se pide solo con su clave. Las aserciones comparan atributos, claves del catálogo
 * y textos exactos, nunca subcadenas con datos (VIG-194). Datos sintéticos.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr07/inventory';
import { text } from '@/texts';
import { effectivePermissions } from '../../generators';
import { server } from '../../msw/server';
import { fleetFixture, hierarchyPlants, node, PLANT, ZONE_A, ZONE_B } from './support';

const WAIT = { timeout: 20_000 };
const ADMIN = effectivePermissions('customer_organization', ['administrator']);

function user(index: number, status: string) {
  return {
    user_id: `0190a1b2-6666-7000-8000-${String(index).padStart(12, '0')}`,
    email: `persona${String(index)}@ejemplo.test`,
    display_name: `Persona sintética ${String(index)}`,
    professional_license: null,
    status,
    second_factor_required: false,
    second_factor_enrolled: false,
    assignments: [],
  };
}

function gateOf(zone_id: string, approved: boolean) {
  return {
    zone_id,
    plant_id: PLANT,
    resulting_mode: approved ? 'productive' : 'no_capture',
    issued_at: null,
    mounting: {
      status: approved ? 'approved' : 'pending',
      decided_at: null,
      decided_by: null,
      record_id: null,
    },
    usage: { status: 'pending', decided_at: null, decided_by: null, agreement_id: null },
  };
}

async function open(permissions: readonly string[] = ADMIN) {
  const reads = { users: 0, gates: 0, nodes: 0 };
  const env = await fleetFixture('/fleet', { plants: hierarchyPlants(), nodes: [] }, permissions);
  server.use(
    http.get('http://localhost/fleet/nodes', () => {
      reads.nodes++;
      const silent = node(2);
      silent.communication_state = 'mute';
      return HttpResponse.json({ nodes: [node(1), silent], next_after: null });
    }),
    http.get('http://localhost/users', () => {
      reads.users++;
      return HttpResponse.json({
        users: [user(1, 'invited'), user(2, 'active'), user(3, 'invited')],
        next_after: null,
      });
    }),
    http.get('http://localhost/zones/:zone_id/gates', ({ params }) => {
      reads.gates++;
      return HttpResponse.json(gateOf(String(params.zone_id), params.zone_id === ZONE_A));
    }),
  );
  const view = render(<Page />, { wrapper: env.Wrapper });
  await screen.findByRole('region', { name: text('scr07.home.pending') }, WAIT);
  await waitFor(() => {
    expect(env.cache.client.isFetching()).toBe(0);
    expect(view.container.querySelector('[aria-busy="true"]')).toBeNull();
  }, WAIT);
  return { ...env, ...view, reads };
}

function pending(container: HTMLElement) {
  return [...container.querySelectorAll('[data-pending]')].map((item) => [
    item.getAttribute('data-pending'),
    item.getAttribute('data-value'),
    item.querySelector('a')?.getAttribute('href') ?? null,
  ]);
}

describe('SCR-07 inicio del administrador (VIG-365)', { timeout: 40_000 }, () => {
  it('«Pendiente de su parte» con su recuento y su botón, y la flota debajo', async () => {
    const { container, reads } = await open();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('scr07.home.title'));
    expect(pending(container)).toEqual([
      ['invitations', '2', '/admin/users'],
      ['charters', '1', `/zones/${ZONE_B}/gates`],
      ['silent_nodes', '1', '/fleet/alarms'],
    ]);
    // Una sola lectura del inventario: la flota de abajo comparte la del pendiente.
    expect(reads.nodes).toBe(1);
    expect(screen.getByRole('link', { name: 'NODO-SINT-1' })).toBeDefined();
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }),
    ).toHaveLength(1);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('las áreas de configuración son las del menú del rol, sin el inicio ni el expediente', async () => {
    await open();
    const areas = screen.getByRole('region', { name: text('scr07.home.areas') });
    const names = [...areas.querySelectorAll('li')].map(
      (item) => item.querySelector('a, h3')?.textContent,
    );
    expect(names).toEqual(
      [
        'navigation.menu.users',
        'navigation.menu.hierarchy',
        'navigation.menu.organization',
        'navigation.menu.gates',
        'navigation.menu.admissions',
        'navigation.menu.catalog',
        'navigation.menu.vocabulary',
        'navigation.menu.policy',
      ].map(text),
    );
    // Las que piden zona o planta se eligen con un selector, sin consultas en la dirección.
    const gates = within(areas).getByRole('form', { name: text('navigation.menu.gates') });
    expect(
      within(gates).getByRole('combobox', { name: text('role_navigation.zone') }),
    ).toBeDefined();
  });

  it('cada pendiente se pide solo con su clave: sin users.manage ni catalog.read, solo la flota', async () => {
    const { container, reads } = await open(
      ADMIN.filter((key) => key !== 'users.manage' && key !== 'catalog.read'),
    );
    expect(reads.users).toBe(0);
    expect(reads.gates).toBe(0);
    expect(pending(container).map(([id]) => id)).toEqual(['silent_nodes']);
  });

  it('con coordinador y administrador, el inicio es el del coordinador: /fleet es el inventario', async () => {
    // BR-APP-04: el inicio es el del primer rol de HOME_ROLE_ORDER, entre todas las pantallas.
    const env = await fleetFixture(
      '/fleet',
      { plants: hierarchyPlants(), nodes: [] },
      effectivePermissions('customer_organization', ['coordinator_sst', 'administrator']),
      ['administrator', 'coordinator_sst'],
    );
    server.use(
      http.get('http://localhost/fleet/nodes', () =>
        HttpResponse.json({ nodes: [node(1)], next_after: null }),
      ),
    );
    const view = render(<Page />, { wrapper: env.Wrapper });
    await screen.findByRole('link', { name: 'NODO-SINT-1' }, WAIT);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('scr07.title'));
    expect(view.container.querySelector('[data-pending]')).toBeNull();
  });

  it('con solo las claves de la flota, el único pendiente es el de los nodos', async () => {
    const env = await fleetFixture('/fleet', { plants: hierarchyPlants(), nodes: [] });
    server.use(
      http.get('http://localhost/fleet/nodes', () =>
        HttpResponse.json({ nodes: [node(1)], next_after: null }),
      ),
    );
    const view = render(<Page />, { wrapper: env.Wrapper });
    await screen.findByRole('link', { name: 'NODO-SINT-1' }, WAIT);
    expect(view.container.querySelectorAll('[data-pending]')).toHaveLength(1);
  });
});
