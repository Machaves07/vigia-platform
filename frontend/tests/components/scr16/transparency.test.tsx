/**
 * SCR-16 · vista de transparencia (IF-19, RV-20; BR-APP-21, 22, 65 y 66): alcance declarado,
 * estándares, acuerdo vigente con firmantes y fechas, compuertas y vista en vivo con su
 * explicación; ningún hallazgo, contador ni dato de gestión aunque la respuesta los traiga; en
 * tableta (768 × 1024) los controles miden ≥ 44 px y `axe` no encuentra violaciones serias.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import Entry from '@/pages/scr16/entry';
import Zone from '@/pages/scr16/zone';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import { AGREEMENT, INSTANT, transparency, transparencyFixture, WAIT, ZONE } from './support';

// vitest no procesa los módulos CSS (`styles` llega vacío): cada clase se llama como en el fuente,
// para poder inyectar `scr16.module.css` tal cual en la prueba de tableta.
vi.mock('@/pages/scr16/scr16.module.css', () => ({
  default: new Proxy({}, { get: (_target, key) => key }),
}));

/** Hojas de estilo reales de la aplicación: jsdom no procesa el CSS importado. */
function stylesheet(...path: string[]): string {
  const src = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'src');
  return readFileSync(join(src, ...path), 'utf8');
}
const baseCss = stylesheet('styles', 'base.css');
const tokensCss = stylesheet('styles', 'tokens.css');
const screenCss = stylesheet('pages', 'scr16', 'scr16.module.css');

const URL_TRANSPARENCY = `http://localhost/zones/${ZONE}/transparency`;

function serve(body: Parameters<typeof HttpResponse.json>[0]) {
  server.use(http.get(URL_TRANSPARENCY, () => HttpResponse.json(body)));
}

async function renderZone(body: Parameters<typeof HttpResponse.json>[0]) {
  serve(body);
  const fixture = await transparencyFixture();
  const view = render(<Zone />, { wrapper: fixture.Wrapper });
  await screen.findByRole('heading', { name: text('scr16.scope.title') }, WAIT);
  return view;
}

/** Valores centinela de gestión: no pueden aparecer en el texto ni en ningún atributo. */
const MANAGEMENT = {
  findings: [{ finding_state_id: 'HALLAZGO-INYECTADO-0001', title_es: 'HALLAZGOOCULTO1' }],
  open_findings_count: 98765431,
  people_count: 13572469,
  overdue_actions: 24681359,
} as const;

function rendered(container: HTMLElement): string[] {
  const found = [container.textContent];
  for (const element of container.querySelectorAll('*')) {
    for (const attribute of element.attributes) found.push(attribute.value);
  }
  return found;
}

function leaked(container: HTMLElement, values: readonly string[]): string[] {
  const haystack = rendered(container);
  return values.filter((value) => haystack.some((entry) => entry.includes(value)));
}

describe('SCR-16 vista de transparencia', { timeout: 30_000 }, () => {
  it('pinta alcance, estándares, acuerdo con firmantes y fechas, compuertas y vista en vivo', async () => {
    const { container } = await renderZone(transparency());
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('navigation.scr16'));
    expect(container.querySelector('[data-part="zone"]')?.textContent).toBe(
      `${text('scr16.zone')} Zona sintética`,
    );
    expect(screen.getByText('Alcance sintético de la zona de prensas')).toBeDefined();
    const camera = container.querySelector('[data-part="camera"]');
    expect(camera?.getAttribute('data-required')).toBe('true');
    expect(
      within(camera as HTMLElement).getByText(text('scr16.scope.reference_marker')),
    ).toBeDefined();
    expect(container.querySelector('[data-part="minimum_coverage"]')?.textContent).toBe(
      `${text('scr16.scope.minimum_coverage_cameras')} 1`,
    );
    expect(screen.getByRole('heading', { name: 'Estándar sintético de resguardo' })).toBeDefined();
    expect(screen.getByText('Texto declarado sintético del estándar')).toBeDefined();
    expect(screen.getByText(label('agreement_status', 'approved'))).toBeDefined();
    const signatories = screen.getByRole('table', { name: text('scr16.agreement.signatories') });
    const [, confirmed, pending, ...rest] = within(signatories).getAllByRole('row');
    expect(rest).toEqual([]);
    if (confirmed === undefined || pending === undefined) throw new Error('Faltan firmantes.');
    expect(within(confirmed).getByText(label('role', 'copasst'))).toBeDefined();
    expect(within(confirmed).getByText('Firmante sintético')).toBeDefined();
    expect(confirmed.querySelector(`time[datetime="${INSTANT}"]`)).not.toBeNull();
    expect(within(pending).getByText(text('scr16.agreement.unnamed'))).toBeDefined();
    expect(within(pending).getByText(text('scr16.agreement.pending'))).toBeDefined();
    expect(
      screen.getByRole('group', { name: text('components_status.gate.group_label') }).dataset.state,
    ).toBe('productive');
    expect(screen.getByText(label('zone_mode', 'productive'))).toBeDefined();
    const launcher = screen.getByRole('region', { name: text('live_view.region_label') });
    for (const key of ['live_view.blurred', 'live_view.local_network', 'live_view.access_recorded'])
      expect(launcher.querySelector(`[data-text-key="${key}"]`)).not.toBeNull();
    expect(container.querySelector('[data-part="updated"]')).not.toBeNull();
    expect(container.querySelector('[data-part="confirm"]')).toBeNull();
    expect(leaked(container, [AGREEMENT, ZONE])).toEqual([]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('con hallazgos, contadores y datos de gestión inyectados en la respuesta no los pinta', async () => {
    const base = transparency();
    const agreement = base.current_agreement;
    const body = {
      ...base,
      ...MANAGEMENT,
      declared_scope: { ...base.declared_scope, detections_total: 86420975 },
      standards: base.standards.map((standard) => ({ ...standard, findings_open: 75319864 })),
      current_agreement:
        agreement === null
          ? null
          : {
              ...agreement,
              signatories: agreement.signatories.map((signatory) => ({
                ...signatory,
                actions_assigned: 11223349,
              })),
            },
      gates: { ...base.gates, findings_blocked: 55667781 },
    };
    const { container } = await renderZone(body);
    expect(
      leaked(container, [
        'HALLAZGO-INYECTADO-0001',
        'HALLAZGOOCULTO1',
        '98765431',
        '13572469',
        '24681359',
        '86420975',
        '75319864',
        '11223349',
        '55667781',
      ]),
    ).toEqual([]);
    expect(screen.getByText('Alcance sintético de la zona de prensas')).toBeDefined();
  });

  it.each([
    [
      'agreement_revoked',
      {
        usage: {
          status: 'revoked',
          decided_at: INSTANT,
          agreement_id: AGREEMENT,
          decided_by: null,
        },
      },
    ],
    [
      'no_scope_record',
      { mounting: { status: 'pending', decided_at: null, record_id: null, decided_by: null } },
    ],
    ['commissioning', { resulting_mode: 'commissioning' }],
    [
      'no_use_agreement',
      {
        resulting_mode: 'no_capture',
        usage: { status: 'pending', decided_at: null, agreement_id: null, decided_by: null },
      },
    ],
  ])('compuertas como GateStateChip «%s»', async (state, patch) => {
    const base = transparency();
    await renderZone({ ...base, gates: { ...base.gates, ...patch } });
    expect(
      screen.getByRole('group', { name: text('components_status.gate.group_label') }).dataset.state,
    ).toBe(state);
  });

  it('sin acta, sin catálogo y sin acuerdo lo dice con el catálogo de textos', async () => {
    await renderZone(
      transparency({
        declared_scope: { scope_text_es: null, cameras: [], minimum_coverage: null },
        standards: [],
        current_agreement: null,
      }),
    );
    for (const key of [
      'scr16.scope.no_record',
      'scr16.scope.no_cameras',
      'scr16.scope.no_minimum_coverage',
      'scr16.standards.empty',
      'scr16.agreement.none',
    ])
      expect(screen.getByText(text(key))).toBeDefined();
  });

  it('una zona fuera del alcance usa el estado común sin datos de la zona', async () => {
    server.use(http.get(URL_TRANSPARENCY, () => apiError('not_found', 404)));
    const fixture = await transparencyFixture();
    render(<Zone />, { wrapper: fixture.Wrapper });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('heading', { name: text('scr16.scope.title') })).toBeNull();
  });

  it('el inicio del COPASST enlaza cada zona de su alcance', async () => {
    const fixture = await transparencyFixture('/transparency');
    const { container } = render(<Entry />, { wrapper: fixture.Wrapper });
    const link = await screen.findByRole('link', { name: 'Zona sintética' }, WAIT);
    expect(link.getAttribute('href')).toBe(`/transparency/${ZONE}`);
    expect(container.querySelector('[data-part="updated"]')).not.toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it.each([
    [
      'la vista de zona',
      async () =>
        (
          await renderZone(
            transparency({ pending_confirmation_for_me: '0190a1b2-1616-7000-8000-0000000000a2' }),
          )
        ).container,
    ],
    [
      'el inicio',
      async () => {
        const fixture = await transparencyFixture('/transparency');
        const { container } = render(<Entry />, { wrapper: fixture.Wrapper });
        await screen.findByRole('link', { name: 'Zona sintética' }, WAIT);
        return container;
      },
    ],
  ])('en tableta (768 × 1024) cada control de %s mide al menos 44 px', async (_name, mount) => {
    const width = window.innerWidth;
    const height = window.innerHeight;
    window.innerWidth = 768;
    window.innerHeight = 1024;
    const touch = /--touch-target-min:\s*([^;]+);/.exec(tokensCss)?.[1]?.trim();
    expect(touch).toBe('44px');
    const style = document.createElement('style');
    style.textContent = `${baseCss}\n${screenCss}`.replaceAll(
      'var(--touch-target-min)',
      touch ?? '0',
    );
    document.head.append(style);
    try {
      const container = await mount();
      const controls = [...container.querySelectorAll('a, button, select, input, [role="button"]')];
      expect(controls.length).toBeGreaterThanOrEqual(2);
      // `min-height` no se aplica a una caja en línea: en el navegador un enlace sin clase mide
      // la altura de su línea (19 px), aunque jsdom informe los 44 px de `base.css`.
      const small = controls
        .filter((control) => {
          const computed = getComputedStyle(control);
          return (
            computed.display === 'inline' ||
            parseFloat(computed.minHeight) < 44 ||
            parseFloat(computed.minWidth) < 44
          );
        })
        .map((control) => control.outerHTML.slice(0, 80));
      expect(small).toEqual([]);
      expect(await axe(container)).toHaveNoViolations();
    } finally {
      style.remove();
      window.innerWidth = width;
      window.innerHeight = height;
    }
  });
});
