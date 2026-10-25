/**
 * SCR-05 · estado de compuertas (IF-11 pasos 1, 3 y 6; RV-08; BR-APP-31, 43):
 *
 * - Zona y planta pintan montaje, uso, modo resultante y «actualizado hace X» con `GateStateChip`.
 * - El aviso «planta sin política de incerrables» es informativo: el árbol de controles (nombre,
 *   tipo y si está deshabilitado) es idéntico con y sin política cargada.
 * - La revocación exige motivo (10 a 500) y una confirmación explícita aparte; nada se envía sin las
 *   dos, y `axe` no encuentra violaciones en ninguna de las dos fases.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Gates from '@/pages/scr05/gates';
import { text } from '@/texts';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  chips,
  gateState,
  gatesFixture,
  INSTALLER,
  ORIGIN,
  part,
  OTHER_ZONE,
  PENDING,
  PLANT,
  plantPolicy,
  serveReads,
  transparency,
  WAIT,
  ZONE,
  type Mode,
} from './support';

const ZONE_PATH = `/zones/${ZONE}/gates`;
const PLANT_PATH = `/plants/${PLANT}/gates`;
const REASON = 'Motivo sintético de la revocación de prueba';

async function renderAt(path: string, permissions: readonly string[] = INSTALLER) {
  const fixture = await gatesFixture(path, permissions);
  return { ...fixture, ...render(<Gates />, { wrapper: fixture.Wrapper }) };
}

/** Nombre accesible simple de un control: su etiqueta, su `aria-label` o su texto. */
function controlName(element: Element): string {
  const labelled = (element as HTMLInputElement).labels?.[0]?.textContent;
  return (labelled ?? element.getAttribute('aria-label') ?? element.textContent).trim();
}

/** El árbol de controles de la pantalla: qué acción es y si se puede usar. */
function controls(root: HTMLElement): string[] {
  return [...root.querySelectorAll('button, input, select, textarea, a[href]')].map((element) => {
    const disabled =
      (element as HTMLButtonElement).disabled || element.closest('fieldset:disabled') !== null;
    return `${element.tagName}:${element.getAttribute('type') ?? ''}:${controlName(element)}:${String(disabled)}`;
  });
}

const notice = (root: HTMLElement) => root.querySelector('[data-part="policy_notice"]');

describe('SCR-05 estado de compuertas', { timeout: 40_000 }, () => {
  it('la zona pinta montaje, uso, modo resultante y «actualizado hace X» sin violaciones de axe', async () => {
    serveReads({ gates: () => gateState('productive') });
    const { container } = await renderAt(ZONE_PATH);
    await waitFor(() => {
      expect(chips(container)).toEqual(['productive']);
    }, WAIT);
    const panel = part(container, 'gate_state');
    expect(within(panel).getByText(text('scr05.gates.mounting'))).toBeDefined();
    expect(within(panel).getByText(text('scr05.gates.usage'))).toBeDefined();
    expect(panel.querySelector('[data-part="resulting_mode"]')?.textContent).not.toBe('');
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }).length,
    ).toBeGreaterThan(0);
    expect(container.querySelector('[data-part="zone"]')?.textContent).toBe(
      `${text('scr05.zone')} Zona de prensas`,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it.each<[Mode, string]>([
    ['pending', 'no_scope_record'],
    ['commissioning', 'no_use_agreement'],
    ['productive', 'productive'],
    ['usage_revoked', 'agreement_revoked'],
  ])('el chip refleja lo que decidió el servidor (%s → %s)', async (mode, state) => {
    serveReads({ gates: () => gateState(mode) });
    const { container } = await renderAt(ZONE_PATH);
    await waitFor(() => {
      expect(chips(container)).toEqual([state]);
    }, WAIT);
  });

  it('la planta lista sus zonas con su chip y la política de firmantes', async () => {
    serveReads({
      gates: (zoneId) => gateState(zoneId === OTHER_ZONE ? 'pending' : 'productive', zoneId),
    });
    const { container } = await renderAt(PLANT_PATH);
    await waitFor(() => {
      expect(chips(container)).toEqual(['productive', 'no_scope_record']);
    }, WAIT);
    expect(
      [...container.querySelectorAll('[data-part="zone_row"] h3')].map((node) => node.textContent),
    ).toEqual(['Zona de prensas', 'Zona de empaque']);
    await waitFor(() => {
      expect(container.querySelector('[data-part="signatory_policy"]')).not.toBeNull();
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('SCR-05 aviso de planta sin política (informativo)', { timeout: 60_000 }, () => {
  it.each([
    ['zona', ZONE_PATH],
    ['planta', PLANT_PATH],
  ])(
    'en la %s el aviso aparece y no deshabilita ni oculta ninguna acción',
    async (_where, path) => {
      const tree = async (loaded: boolean) => {
        serveReads({ policy: plantPolicy(loaded), transparency: () => transparency(PENDING) });
        const view = await renderAt(path);
        const { container } = view;
        await waitFor(() => {
          expect(container.querySelector('[data-part="gate_state"]')).not.toBeNull();
          if (path === ZONE_PATH) {
            expect(container.querySelector('[data-part="agreement_form"]')).not.toBeNull();
            expect(container.querySelector('[data-part="confirm"]')).not.toBeNull();
            expect(container.querySelector('[data-part="approval"]')).not.toBeNull();
          } else {
            expect(container.querySelector('[data-part="signatory_form"]')).not.toBeNull();
          }
          expect(notice(container) === null).toBe(loaded);
        }, WAIT);
        // La política ya llegó en los dos casos: el aviso está o no está por la respuesta.
        const result = { controls: controls(container), notice: notice(container) };
        const noticeLinks = result.notice === null ? [] : controls(result.notice as HTMLElement);
        view.unmount();
        return { ...result, noticeLinks };
      };
      const without = await tree(false);
      const withPolicy = await tree(true);
      expect(without.notice).not.toBeNull();
      expect(without.notice?.getAttribute('role')).toBeNull();
      // El aviso solo añade su enlace a la carga de la política; el resto del árbol es idéntico.
      expect(without.noticeLinks).toEqual([`A::${text('scr05.policy_notice.link')}:false`]);
      expect(without.controls.filter((control) => !without.noticeLinks.includes(control))).toEqual(
        withPolicy.controls,
      );
    },
  );
});

describe('SCR-05 revocación de compuerta', { timeout: 40_000 }, () => {
  it.each<['mounting' | 'usage']>([['usage'], ['mounting']])(
    'exige motivo y confirmación explícita antes de revocar %s',
    async (gate) => {
      let mode: Mode = 'productive';
      serveReads({ gates: () => gateState(mode) });
      const sent: { gate: string; body: unknown }[] = [];
      server.use(
        http.post(
          `${ORIGIN}/zones/:zone_id/gates/:gate/revocation`,
          async ({ request, params }) => {
            sent.push({ gate: String(params.gate), body: await request.json() });
            mode = 'usage_revoked';
            return HttpResponse.json(gateState('usage_revoked'));
          },
        ),
      );
      const { container } = await renderAt(ZONE_PATH);
      const section = await waitFor(() => {
        const node = container.querySelector('[data-part="revocation"]');
        expect(node).not.toBeNull();
        return node as HTMLElement;
      }, WAIT);
      const reason = within(section).getByRole('textbox');
      const request = () =>
        within(section).getByRole('button', { name: text('scr05.revocation.request') });
      fireEvent.click(within(section).getByRole('radio', { name: text(`scr05.gates.${gate}`) }));

      // Sin motivo: error local y nada se envía ni se pide confirmar.
      fireEvent.click(request());
      await waitFor(() => {
        expect(reason.getAttribute('aria-invalid')).toBe('true');
      }, WAIT);
      expect(section.querySelector('[data-part="revocation_confirm"]')).toBeNull();
      // Motivo por debajo del mínimo de U-03 (10): igual.
      fireEvent.change(reason, { target: { value: 'corto' } });
      fireEvent.click(request());
      expect(section.querySelector('[data-part="revocation_confirm"]')).toBeNull();

      // Con motivo, pide la confirmación explícita y todavía no envía nada.
      fireEvent.change(reason, { target: { value: REASON } });
      fireEvent.click(request());
      const confirm = await within(section).findByRole(
        'group',
        { name: text('scr05.revocation.confirm_title') },
        WAIT,
      );
      expect(within(confirm).getByText(text(`scr05.revocation.confirm_${gate}`))).toBeDefined();
      expect(sent).toEqual([]);
      expect(await axe(container)).toHaveNoViolations();

      // Cancelar no envía.
      fireEvent.click(
        within(confirm).getByRole('button', { name: text('scr05.revocation.cancel') }),
      );
      expect(section.querySelector('[data-part="revocation_confirm"]')).toBeNull();
      expect(sent).toEqual([]);

      // Confirmar envía la compuerta elegida con el motivo y el chip refleja la respuesta.
      fireEvent.click(request());
      fireEvent.click(
        await within(section).findByRole(
          'button',
          { name: text('scr05.revocation.confirm') },
          WAIT,
        ),
      );
      await waitFor(() => {
        expect(sent).toHaveLength(1);
      }, WAIT);
      expect(sent).toEqual([{ gate, body: { reason_es: REASON } }]);
      await waitFor(() => {
        expect(chips(container)).toEqual(['agreement_revoked']);
      }, WAIT);
      await within(section).findByText(text('scr05.revocation.done'), {}, WAIT);
      expect(await axe(container)).toHaveNoViolations();
    },
  );

  it('una compuerta que no está aprobada responde conflict y se ancla a la revocación', async () => {
    serveReads({ gates: () => gateState('commissioning') });
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/gates/:gate/revocation`, () => apiError('conflict', 409)),
    );
    const { container } = await renderAt(ZONE_PATH);
    const section = await waitFor(() => {
      const node = container.querySelector('[data-part="revocation"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    fireEvent.change(within(section).getByRole('textbox'), { target: { value: REASON } });
    fireEvent.click(
      within(section).getByRole('button', { name: text('scr05.revocation.request') }),
    );
    fireEvent.click(
      await within(section).findByRole('button', { name: text('scr05.revocation.confirm') }, WAIT),
    );
    const alert = await within(section).findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('conflict');
    expect(chips(container)).toEqual(['no_use_agreement']);
  });
});
