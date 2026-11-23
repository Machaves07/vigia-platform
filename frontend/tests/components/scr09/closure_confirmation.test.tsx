/**
 * Recorrido de cierre M10 de U-06 (O-17 y O-18): el efecto de cada acción se ve.
 *
 * - O-17: tras registrar la evidencia hay un acuse con qué, cuándo y cargo, el formulario queda
 *   vacío y el foco va al acuse; un envío igual al último de la misma acción (descripción
 *   recortada, hora de ejecución, cargo y nota) pide confirmación antes de salir, tanto si la
 *   anterior se registró desde esta página como si ya estaba en el expediente.
 * - O-18: con el lazo cerrado, descartado o incerrable no se ofrecen los formularios; arriba,
 *   «Lazo cerrado el … por …» y los enlaces que siguen. Firmado aquí, el aviso lo confirma.
 *
 * Datos sintéticos; el reloj del transporte es el inyectado (START, 25-10-2026 02:00 UTC).
 */
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { plantTime } from '@/components/TimestampWithOffset/format';
import type { Detail } from '@/ports/scr09Closure';
import { text } from '@/texts';
import { exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { changeDateTime } from '../../support/dateFields';
import {
  ACTION,
  DESCRIPTION,
  FINDING,
  INSTANT,
  PERMISSIONS,
  WAIT,
  detail,
  ledgerRecord,
  load,
  reasonName,
  renderClosure,
} from './closure_support';

type User = Awaited<ReturnType<typeof load>>;

/** Ejecutada el 24-10-2026 a las 10:00 en Bogotá. */
const PERFORMED = '2026-10-24T15:00:00.000Z';
/** Una clasificación anterior a la última. */
const START_ISO = '2026-10-20T15:00:00.000Z';
const ROLE = 'Mantenimiento';

async function fill(user: User, role = ROLE) {
  await user.type(
    screen.getByRole('textbox', { name: reasonName('scr09.closure.description') }),
    DESCRIPTION,
  );
  changeDateTime(screen.getByLabelText(text('scr09.closure.performed_at')), '2026-10-24T10:00');
  await user.type(screen.getByLabelText(text('scr09.closure.performed_by_role')), role);
}

async function submitEvidence(user: User) {
  await user.click(screen.getByRole('button', { name: text('scr09.closure.submit_evidence') }));
}

/** El servidor guarda cada cuerpo y lo da por registrado a las 21:00 de Bogotá (INSTANT). */
function serveEvidence() {
  const bodies: unknown[] = [];
  server.use(
    http.post(`http://localhost/loop/actions/${ACTION}/evidence`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(
        {
          ...(exampleFor('add_evidence_loop_actions__action_id__evidence_post') as object),
          action_id: ACTION,
          added_at: INSTANT,
          performed_at: PERFORMED,
        },
        { status: 201 },
      );
    }),
  );
  return bodies;
}

function form() {
  return screen.getByRole('form', { name: text('scr09.closure.evidence') });
}

/** Lo que dice el acuse, campo a campo. */
function receipt() {
  const node = form().querySelector<HTMLElement>('[data-part="evidence_receipt"]');
  if (node === null) return null;
  return {
    role: node.getAttribute('role'),
    title: node.querySelector('[data-part="receipt_title"]')?.textContent,
    what: node.querySelector('[data-part="receipt_what"]')?.textContent,
    when: node.querySelector('[data-part="receipt_when"] time')?.getAttribute('datetime'),
    cargo: node.querySelector('[data-part="receipt_role"]')?.textContent,
    focused: document.activeElement === node,
  };
}

/** Los valores del formulario de evidencia. */
function values() {
  const scope = within(form());
  return {
    description: scope.getByRole<HTMLTextAreaElement>('textbox', {
      name: reasonName('scr09.closure.description'),
    }).value,
    verification: scope.getByRole<HTMLTextAreaElement>('textbox', {
      name: `${text('scr09.closure.verification')} (${text('components_forms.reason.optional')})`,
    }).value,
    performed: scope.getByLabelText<HTMLInputElement>(text('scr09.closure.performed_at')).value,
    role: scope.getByLabelText<HTMLInputElement>(text('scr09.closure.performed_by_role')).value,
  };
}

const EMPTY = { description: '', verification: '', performed: '', role: '' };
const FILLED = {
  description: DESCRIPTION,
  verification: '',
  // La fecha visible del campo de fecha y hora (`DateTimeInput`).
  performed: '24/10/2026',
  role: ROLE,
};
const REPEAT = `${text('scr09.closure.repeat_notice')} 21:00. ${text('scr09.closure.repeat_question')}`;

function repeatText() {
  return (
    form().querySelector('[data-part="repeat_confirmation"] [data-part="repeat_text"]')
      ?.textContent ?? null
  );
}

describe(
  'O-17: evidencia registrada a la vista y sin duplicados por descuido',
  { timeout: 60_000 },
  () => {
    it('tras registrar: acuse con qué, cuándo y cargo, formulario vacío y foco en el acuse', async () => {
      const { container } = await renderClosure();
      const user = await load();
      const bodies = serveEvidence();
      expect(receipt()).toBeNull();
      await fill(user);
      await submitEvidence(user);
      await waitFor(() => {
        expect(receipt()).toEqual({
          role: 'status',
          title: text('scr09.closure.evidence_registered'),
          what: DESCRIPTION,
          when: PERFORMED,
          cargo: ROLE,
          focused: true,
        });
      }, WAIT);
      expect(values()).toEqual(EMPTY);
      expect(bodies).toHaveLength(1);
      // El siguiente paso lleva al título de «Cerrar y firmar».
      await user.click(screen.getByRole('button', { name: text('scr09.closure.go_to_sign') }));
      expect(document.activeElement?.textContent).toBe(text('scr09.closure.sign'));
      expect(await axe(container)).toHaveNoViolations();
    });

    it('el mismo envío otra vez pide confirmación; «No registrar» no envía y «Registrar otra igual» sí', async () => {
      await renderClosure();
      const user = await load();
      const bodies = serveEvidence();
      await fill(user);
      await submitEvidence(user);
      await waitFor(() => {
        expect(receipt()?.title).toBe(text('scr09.closure.evidence_registered'));
      }, WAIT);
      await fill(user);
      await submitEvidence(user);
      await waitFor(() => {
        expect(repeatText()).toBe(REPEAT);
      }, WAIT);
      expect(bodies).toHaveLength(1);
      expect(values()).toEqual(FILLED);
      await user.click(screen.getByRole('button', { name: text('scr09.closure.repeat_cancel') }));
      expect(repeatText()).toBeNull();
      expect(bodies).toHaveLength(1);
      expect(values()).toEqual(FILLED);
      await submitEvidence(user);
      await waitFor(() => {
        expect(repeatText()).toBe(REPEAT);
      }, WAIT);
      await user.click(screen.getByRole('button', { name: text('scr09.closure.repeat_confirm') }));
      await waitFor(() => {
        expect(bodies).toHaveLength(2);
      }, WAIT);
      expect(bodies[1]).toEqual(bodies[0]);
      expect(repeatText()).toBeNull();
    });

    it('contra la última evidencia del expediente: igual pide confirmación, otro cargo se envía', async () => {
      await renderClosure('closure', {
        records: [
          ledgerRecord('action_evidence_added', {
            action_id: ACTION,
            description: `  ${DESCRIPTION} `,
            performed_at: PERFORMED,
            performed_by_role: ROLE,
            added_at: INSTANT,
          }),
        ],
      });
      const user = await load();
      const bodies = serveEvidence();
      await fill(user);
      await submitEvidence(user);
      await waitFor(() => {
        expect(repeatText()).toBe(REPEAT);
      }, WAIT);
      expect(bodies).toEqual([]);
      await user.click(screen.getByRole('button', { name: text('scr09.closure.repeat_cancel') }));
      await user.type(screen.getByLabelText(text('scr09.closure.performed_by_role')), ' de planta');
      await submitEvidence(user);
      await waitFor(() => {
        expect(bodies).toHaveLength(1);
      }, WAIT);
      expect(repeatText()).toBeNull();
    });
  },
);

/** Lo que la página dice arriba del lazo terminado y lo que ofrece. */
function ending() {
  const region = screen.queryByRole('region', { name: text('scr09.closure.ended.label') });
  if (region === null) return null;
  const summary = region.querySelector<HTMLElement>('[data-part="ended_summary"]');
  return {
    summary: summary?.textContent,
    at: summary?.querySelector('time')?.getAttribute('datetime') ?? null,
    links: within(region)
      .queryAllByRole('link')
      .map((link) => [link.textContent, link.getAttribute('href')]),
  };
}

function offered() {
  return {
    evidence: screen.queryByRole('form', { name: text('scr09.closure.evidence') }) !== null,
    close: screen.queryByRole('region', { name: text('scr09.closure.sign') }) !== null,
  };
}

/** INSTANT en la planta (Bogotá) con su zona, como lo pinta `TimestampWithOffset`. */
function stamp(): string {
  const time = plantTime(INSTANT, 'America/Bogota');
  if (time === null) throw new Error('Instante sintético no válido.');
  return `${time.local} (America/Bogota, ${time.offset})`;
}
const ON = text('scr09.closure.ended.on');
const BY = text('scr09.closure.ended.by');

describe(
  'O-18: el lazo terminado se dice arriba y no ofrece los formularios',
  { timeout: 60_000 },
  () => {
    const lifecycle = (state: Detail['lifecycle_state'], extra: Partial<Detail> = {}): Detail => ({
      ...detail,
      lifecycle_state: state,
      ...extra,
    });

    it('con el lazo abierto todo sigue igual: formularios y ningún aviso de fin', async () => {
      await renderClosure();
      await load();
      expect(ending()).toBeNull();
      expect(offered()).toEqual({ evidence: true, close: true });
    });

    it('cerrado: «Lazo cerrado el … por <firma>», causa raíz, ficha de vida y reabrir', async () => {
      await renderClosure('closure', {
        detail: lifecycle('closed'),
        permissions: [...PERMISSIONS, 'root_cause.analyze'],
      });
      await load();
      await waitFor(() => {
        expect(ending()?.at).toBe(INSTANT);
      }, WAIT);
      expect(ending()).toEqual({
        summary: `${text('scr09.closure.ended.closed')} ${ON} ${stamp()} ${BY} Firma sintética`,
        at: INSTANT,
        links: [
          [text('scr09.closure.ended.root_cause'), `/findings/${FINDING}/root-cause`],
          [text('scr09.closure.ended.timeline'), `/findings/${FINDING}/timeline`],
          [text('scr09.closure.ended.reopen'), `/findings/${FINDING}/loop/reopening`],
        ],
      });
      expect(offered()).toEqual({ evidence: false, close: false });
    });

    it('descartado: quién clasificó y cuándo, sin «Reabrir» ni formularios', async () => {
      const shown = lifecycle('discarded');
      const [first] = shown.classifications;
      if (first === undefined) throw new Error('Falta la clasificación sintética.');
      const by = { ...first.classified_by, display_name: 'Clasificación sintética' };
      await renderClosure('closure', {
        detail: {
          ...shown,
          classifications: [
            { ...first, sequence_in_subject: 1, classified_at: START_ISO, classified_by: by },
            { ...first, sequence_in_subject: 2, classified_at: INSTANT, classified_by: by },
          ],
        },
      });
      await load();
      await waitFor(() => {
        expect(ending()).toEqual({
          summary: `${text('scr09.closure.ended.discarded')} ${ON} ${stamp()} ${BY} Clasificación sintética`,
          at: INSTANT,
          links: [[text('scr09.closure.ended.timeline'), `/findings/${FINDING}/timeline`]],
        });
      }, WAIT);
      expect(offered()).toEqual({ evidence: false, close: false });
    });

    it('incerrable: la declaración firmada y «Reabrir», sin formularios', async () => {
      await renderClosure('closure', {
        detail: lifecycle('unclosable'),
        records: [
          ledgerRecord('unclosable_state_declared', {
            declaration_id: ACTION,
            declared_at: INSTANT,
            signer: { display_name: 'Declaración sintética', professional_license: 'L' },
          }),
        ],
      });
      await load();
      await waitFor(() => {
        expect(ending()?.at).toBe(INSTANT);
      }, WAIT);
      expect(ending()).toEqual({
        summary: `${text('scr09.closure.ended.unclosable')} ${ON} ${stamp()} ${BY} Declaración sintética`,
        at: INSTANT,
        links: [
          [text('scr09.closure.ended.timeline'), `/findings/${FINDING}/timeline`],
          [text('scr09.closure.ended.reopen'), `/findings/${FINDING}/loop/reopening`],
        ],
      });
      expect(offered()).toEqual({ evidence: false, close: false });
    });

    it('al firmar aquí: aviso de cierre registrado con el foco y sin formularios, aunque el detalle tarde', async () => {
      const { container } = await renderClosure('closure', { records: [] });
      const user = await load();
      server.use(
        http.post(`http://localhost/loop/findings/${FINDING}/closure`, () =>
          HttpResponse.json(
            {
              ...(exampleFor('close_loop_findings__finding_state_id__closure_post') as object),
              signed_at: INSTANT,
            },
            { status: 201 },
          ),
        ),
      );
      await user.type(
        screen.getByRole('textbox', { name: reasonName('scr09.closure.sign_description') }),
        DESCRIPTION,
      );
      await user.click(
        screen.getByRole('button', { name: text('components_forms.signature.sign') }),
      );
      await waitFor(() => {
        expect(offered()).toEqual({ evidence: false, close: false });
      }, WAIT);
      const region = screen.getByRole('region', { name: text('scr09.closure.ended.label') });
      // La firma es la persona en sesión (`meResponse`); el detalle aún dice «overdue».
      expect(ending()).toEqual({
        summary: `${text('scr09.closure.ended.just_closed')} ${text('scr09.closure.ended.closed')} ${ON} ${stamp()} ${BY} Persona sintética`,
        at: INSTANT,
        links: [
          [text('scr09.closure.ended.timeline'), `/findings/${FINDING}/timeline`],
          [text('scr09.closure.ended.reopen'), `/findings/${FINDING}/loop/reopening`],
        ],
      });
      expect(document.activeElement).toBe(region.querySelector('[data-part="ended_summary"]'));
      expect(await axe(container)).toHaveNoViolations();
    });
  },
);
