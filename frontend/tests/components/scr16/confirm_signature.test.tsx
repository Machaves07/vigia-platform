/**
 * SCR-16 · «confirmar mi firma» (IF-19 paso 2, BR-APP-52): la acción solo aparece cuando la
 * respuesta trae `pending_confirmation_for_me`, envía **ese** `agreement_id` (nunca el del acuerdo
 * vigente) sin cuerpo, muestra el acuse con el rol que devuelve el servidor y deja de ofrecerse
 * cuando el servidor ya no la pide. Un rechazo se muestra anclado, con su etiqueta.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Zone from '@/pages/scr16/zone';
import { text } from '@/texts';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  AGREEMENT,
  INSTANT,
  PENDING,
  transparency,
  transparencyFixture,
  USER,
  WAIT,
  ZONE,
} from './support';

const URL_TRANSPARENCY = `http://localhost/zones/${ZONE}/transparency`;
const OTHER_PENDING = '0190a1b2-1616-7000-8000-0000000000a3';

interface Sent {
  readonly agreementId: string;
  readonly body: string;
}

/** Transparencia que pide la firma hasta que se confirma; después, ya no. */
function serveFlow(pending: string | null) {
  let current = pending;
  const sent: Sent[] = [];
  server.use(
    http.get(URL_TRANSPARENCY, () =>
      HttpResponse.json(transparency({ pending_confirmation_for_me: current })),
    ),
    http.post(
      'http://localhost/use-agreements/:agreement_id/confirmations',
      async ({ request, params }) => {
        const agreementId = String(params.agreement_id);
        sent.push({ agreementId, body: await request.text() });
        current = null;
        return HttpResponse.json(
          {
            agreement_id: agreementId,
            user_id: USER,
            role_in_use: 'copasst',
            confirmed_at: INSTANT,
            origin: 'transparency',
          },
          { status: 201 },
        );
      },
    ),
  );
  return sent;
}

const action = () => screen.queryByRole('button', { name: text('scr16.confirm.action') });

async function renderZone() {
  const fixture = await transparencyFixture();
  const view = render(<Zone />, { wrapper: fixture.Wrapper });
  await screen.findByRole('heading', { name: text('scr16.scope.title') }, WAIT);
  return view;
}

describe('SCR-16 confirmar mi firma', { timeout: 30_000 }, () => {
  it('sin pending_confirmation_for_me no se ofrece la acción', async () => {
    const sent = serveFlow(null);
    const { container } = await renderZone();
    expect(action()).toBeNull();
    expect(container.querySelector('[data-part="confirm"]')).toBeNull();
    expect(sent).toEqual([]);
  });

  it.each([PENDING, OTHER_PENDING])(
    'con pending_confirmation_for_me = %s envía ese agreement_id, sin cuerpo, y muestra el acuse',
    async (pending) => {
      const sent = serveFlow(pending);
      const { container } = await renderZone();
      const button = action();
      expect(button).not.toBeNull();
      const summary = container.querySelector('[data-part="confirm"]');
      expect(summary?.querySelector('h2')?.textContent).toBe(text('scr16.confirm.title'));
      expect(screen.getByText(text('scr16.confirm.summary')).closest('[data-part="confirm"]')).toBe(
        summary,
      );
      expect(await axe(container)).toHaveNoViolations();
      fireEvent.click(button ?? document.body);
      await waitFor(() => {
        expect(sent).toHaveLength(1);
      }, WAIT);
      expect(sent[0]).toEqual({ agreementId: pending, body: '' });
      expect(sent[0]?.agreementId).not.toBe(AGREEMENT);
      const confirmed = await screen.findByRole(
        'region',
        { name: text('scr16.confirm.done') },
        WAIT,
      );
      expect(confirmed.querySelector('[role="status"]')?.getAttribute('data-role-in-use')).toBe(
        'copasst',
      );
      // El refresco tras la escritura ya no trae la confirmación pendiente: la acción desaparece
      // y el acuse se conserva.
      await waitFor(() => {
        expect(action()).toBeNull();
      }, WAIT);
      expect(container.querySelector('[data-part="confirmed"]')).not.toBeNull();
      expect(sent).toHaveLength(1);
    },
  );

  it('un rechazo del servidor se muestra anclado con su código y la acción sigue disponible', async () => {
    server.use(
      http.get(URL_TRANSPARENCY, () =>
        HttpResponse.json(transparency({ pending_confirmation_for_me: PENDING })),
      ),
      http.post('http://localhost/use-agreements/:agreement_id/confirmations', () =>
        apiError('conflict', 409, { detailCode: 'catalog_signatory_not_expected' }),
      ),
    );
    const { container } = await renderZone();
    fireEvent.click(action() ?? document.body);
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('conflict');
    expect(container.querySelector('[data-part="confirmed"]')).toBeNull();
    await waitFor(() => {
      expect(action()?.hasAttribute('disabled')).toBe(false);
    }, WAIT);
  });
});
