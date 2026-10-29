/**
 * VIG-365 (S3-22 y S3-24): la pantalla del lazo carga al abrir, con la consulta auditada como el
 * detalle, y da una sola acción principal en el panel «Siguiente paso». En lazo abierto,
 * «Comprometer una acción» abre el formulario de la misma pantalla; cerrado, solo la reapertura y
 * la franja «Cerrado y firmado»; incerrable, la revisión con la fecha y la medida de su
 * declaración. Una sola actualización por página. Las aserciones comparan atributos, claves del
 * catálogo y textos exactos, nunca subcadenas con datos (VIG-194). Datos sintéticos.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { detail, FINDING, renderScreen, WAIT } from './classify_support';

const PERMISSIONS = [
  'findings.read',
  'findings.classify',
  'actions.manage',
  'closure_attachments.read',
  'findings.close_and_sign',
  'hierarchy.read',
];
const DUE = '2026-12-08T17:00:00.000Z';
const MEASURE = 'Delimitar el acceso con una barrera física temporal en la zona';
const DECLARATION = '0190a1b2-9999-7000-8000-000000000365';

/** Los registros del expediente que sirve el servidor falso, de la organización de la sesión. */
type Records = (organizationId: string) => unknown[];

function ledgerRecord(
  organization_id: string,
  record_type: string,
  content: Record<string, unknown>,
): Record<string, unknown> {
  const page = exampleFor('list_records_ledger_records_get') as {
    items: Record<string, unknown>[];
  };
  const finding = detail();
  return {
    ...(page.items[0] ?? {}),
    organization_id,
    record_type,
    chain_sequence: 9,
    plant_id: finding.plant_id,
    scope: { plant_id: finding.plant_id, zone_id: finding.zone_id, node_id: null },
    content: { subject_id: finding.subject_id, ...content },
  };
}

async function load(lifecycle_state: string, records: Records = () => []) {
  let ledgerReads = 0;
  let detailReads = 0;
  let organization = '';
  server.use(
    http.get(`http://localhost/loop/findings/${FINDING}`, () => {
      detailReads++;
      return HttpResponse.json(detail({ lifecycle_state }));
    }),
    http.get('http://localhost/ledger/records', () => {
      ledgerReads++;
      return HttpResponse.json({ items: records(organization), next_cursor: null });
    }),
  );
  const rendered = await renderScreen('loop', { permissions: PERMISSIONS });
  organization = rendered.organizationId;
  const panel = await screen.findByRole('region', { name: text('next_step.title') }, WAIT);
  return { ...rendered, panel, ledgerReads: () => ledgerReads, detailReads: () => detailReads };
}

/** [acceso, href o `button`, principal] de cada control del panel, en orden. */
function controls(panel: HTMLElement) {
  return [...panel.querySelectorAll('[data-access]')].map((node) => [
    node.getAttribute('data-access'),
    node.tagName === 'A' ? node.getAttribute('href') : 'button',
    node.getAttribute('data-primary'),
  ]);
}

describe('la pantalla del lazo según el estado del servidor (VIG-365)', { timeout: 40_000 }, () => {
  it('abre cargada: sin botón de consultar y con una sola lectura del lazo', async () => {
    const { detailReads } = await load('open');
    expect(screen.queryByRole('button', { name: text('scr09.loop.load') })).toBeNull();
    expect(detailReads()).toBe(1);
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }),
    ).toHaveLength(1);
  });

  it('abierto: «Comprometer una acción» abre el formulario; reclasificar es secundaria', async () => {
    const { panel, container } = await load('open');
    expect(controls(panel)).toEqual([
      ['action', 'button', 'true'],
      ['reclassify', `/findings/${FINDING}/classify`, null],
    ]);
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
    await userEvent
      .setup()
      .click(within(panel).getByRole('button', { name: text('next_step.access.action') }));
    const form = await screen.findByRole('form', { name: text('scr09.loop.action_form') }, WAIT);
    // El formulario se abre limpio: ningún error antes de escribir.
    expect(form.querySelectorAll('[aria-invalid="true"]')).toHaveLength(0);
    expect(within(form).queryAllByRole('alert')).toHaveLength(0);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('cerrado: solo la reapertura, la franja «Cerrado y firmado» y ningún formulario', async () => {
    const { panel, ledgerReads, container } = await load('closed', (organization) => [
      ledgerRecord(organization, 'closure', {
        signed_at: DUE,
        signer: { display_name: 'Coordinación sintética', professional_license: 'SST-SINT-365' },
      }),
    ]);
    expect(controls(panel)).toEqual([['reopening', `/findings/${FINDING}/loop/reopening`, 'true']]);
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
    const banner = await screen.findByRole(
      'region',
      { name: text('scr09.loop.closed_banner') },
      WAIT,
    );
    await waitFor(() => {
      expect(banner.querySelector('dl')).not.toBeNull();
    }, WAIT);
    expect([...banner.querySelectorAll('dt')].map((node) => node.textContent)).toEqual([
      text('scr09.loop.closed_by'),
      text('scr09.loop.closed_at'),
      text('scr09.loop.closed_license'),
    ]);
    const facts = [...banner.querySelectorAll('dd')];
    expect(facts[0]?.textContent).toBe('Coordinación sintética');
    expect(facts[1]?.querySelector('time')?.getAttribute('datetime')).toBe(DUE);
    expect(facts[2]?.textContent).toBe('SST-SINT-365');
    expect(ledgerReads()).toBe(1);
    expect(
      container.querySelector('[data-part="lifecycle_state"]')?.getAttribute('data-value'),
    ).toBe('closed');
  });

  it('incerrable: la revisión principal con la fecha y la medida de su declaración', async () => {
    const { panel, ledgerReads } = await load('unclosable', (organization) => [
      ledgerRecord(organization, 'unclosable_state_declared', {
        declaration_id: DECLARATION,
        review_due_at: DUE,
        compensatory_measure: MEASURE,
      }),
    ]);
    await waitFor(() => {
      expect(panel.querySelector('[data-part="unclosable_commitment"]')).not.toBeNull();
    }, WAIT);
    expect(controls(panel)).toEqual([
      ['unclosable_review', `/findings/${FINDING}/loop/unclosable`, 'true'],
    ]);
    expect(within(panel).getByText(MEASURE).tagName).toBe('DD');
    expect(ledgerReads()).toBe(1);
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
  });
});
