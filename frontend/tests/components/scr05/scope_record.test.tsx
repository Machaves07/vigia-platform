/**
 * SCR-05 · acta de alcance (IF-11 paso 3; U-03 «Versión 1.5», D-2): `StepWizard` con texto,
 * encuadre de las cámaras del catálogo, declaración del difuminado con su captura PNG
 * (`blur_check_capture`) y documento firmado (`scope_record`). Sin comprobación automática del
 * difuminado aquí; `catalog_blur_not_verified` llega del servidor y se ancla; la falta de política
 * de incerrables nunca bloquea el acta.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Gates from '@/pages/scr05/gates';
import { text } from '@/texts';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  CAMERA,
  chips,
  gateState,
  gatesFixture,
  ORIGIN,
  PLANT,
  plantPolicy,
  serveDocuments,
  serveReads,
  syntheticFile,
  WAIT,
  ZONE,
  type Mode,
} from './support';

const SCOPE = 'Alcance sintético: acceso norte de la prensa 3';
const FRAMING = 'Encuadre sintético del acceso norte';

async function renderWizard() {
  const fixture = await gatesFixture(`/zones/${ZONE}/gates`);
  const view = render(<Gates />, { wrapper: fixture.Wrapper });
  const section = await waitFor(() => {
    const node = view.container.querySelector('[data-part="scope_record"]');
    expect(node).not.toBeNull();
    return node as HTMLElement;
  }, WAIT);
  return { ...view, section };
}

const next = (section: HTMLElement) =>
  within(section).getByRole('button', { name: text('components_structure.wizard.next') });

async function fillTextAndCameras(section: HTMLElement) {
  expect(next(section).hasAttribute('disabled')).toBe(true);
  fireEvent.change(within(section).getByLabelText(text('scr05.scope.text')), {
    target: { value: SCOPE },
  });
  await waitFor(() => {
    expect(next(section).hasAttribute('disabled')).toBe(false);
  }, WAIT);
  fireEvent.click(next(section));
  const include = await within(section).findByRole(
    'checkbox',
    { name: `${text('scr05.scope.include_camera')} CAM-01` },
    WAIT,
  );
  expect(next(section).hasAttribute('disabled')).toBe(true);
  fireEvent.click(include);
  fireEvent.change(within(section).getByLabelText(`${text('scr05.scope.framing')} CAM-01`), {
    target: { value: FRAMING },
  });
  fireEvent.click(
    within(section).getByRole('checkbox', {
      name: `${text('scr05.scope.reference_marker')} CAM-01`,
    }),
  );
  await waitFor(() => {
    expect(next(section).hasAttribute('disabled')).toBe(false);
  }, WAIT);
  fireEvent.click(next(section));
}

describe('SCR-05 acta de alcance', { timeout: 60_000 }, () => {
  it('declara el difuminado con captura PNG, sube el documento y envía el acta', async () => {
    let mode: Mode = 'pending';
    serveReads({ gates: () => gateState(mode), policy: plantPolicy(false) });
    const grants = serveDocuments();
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/gates/mounting/scope-record`, async ({ request }) => {
        bodies.push(await request.json());
        mode = 'commissioning';
        return HttpResponse.json(
          {
            record_id: '0190a1b2-0505-7000-8000-0000000000aa',
            plant_policy_loaded_at_signing: false,
            gates: gateState('commissioning'),
          },
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    const { container, section } = await renderWizard();
    await waitFor(() => {
      expect(chips(container)).toEqual(['no_scope_record']);
    }, WAIT);
    await fillTextAndCameras(section);

    // Difuminado: declaración y captura PNG con su concesión `blur_check_capture`.
    fireEvent.click(
      await within(section).findByRole(
        'checkbox',
        { name: text('scr05.scope.blur_declared') },
        WAIT,
      ),
    );
    await user.upload(
      within(section).getByLabelText(text('scr05.scope.blur_capture'), { selector: 'input' }),
      syntheticFile('captura.png', 'image/png'),
    );
    await user.click(within(section).getByRole('button', { name: text('media.upload.submit') }));
    await within(section).findByText(`${text('media.upload.done')}: captura.png`, {}, WAIT);
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(next(section));

    // Documento firmado del acta.
    await user.upload(
      await within(section).findByLabelText(
        text('scr05.scope.document'),
        { selector: 'input' },
        WAIT,
      ),
      syntheticFile('acta.pdf', 'application/pdf'),
    );
    await user.click(within(section).getByRole('button', { name: text('media.upload.submit') }));
    await within(section).findByText(`${text('media.upload.done')}: acta.pdf`, {}, WAIT);
    fireEvent.click(next(section));

    const summary = await waitFor(() => {
      const node = section.querySelector('[data-part="scope_summary"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    expect(within(summary).getByText(text('scr05.scope.document_attached'))).toBeDefined();
    fireEvent.click(
      within(section).getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
    await within(section).findByText(text('scr05.scope.done'), {}, WAIT);
    expect(
      grants.map(({ kind, content_type, plant_id }) => ({ kind, content_type, plant_id })),
    ).toEqual([
      { kind: 'blur_check_capture', content_type: 'image/png', plant_id: PLANT },
      { kind: 'scope_record', content_type: 'application/pdf', plant_id: PLANT },
    ]);
    expect(bodies).toHaveLength(1);
    const body = bodies[0] as {
      scope_text_es: string;
      cameras: unknown[];
      blur_verification: { declared: boolean; capture_document_ref: { content_type: string } };
      document_ref: { content_type: string };
    };
    expect(body.scope_text_es).toBe(SCOPE);
    expect(body.cameras).toEqual([
      { camera_id: CAMERA, framing_description_es: FRAMING, reference_marker: true },
    ]);
    expect(body.blur_verification.declared).toBe(true);
    expect(body.blur_verification.capture_document_ref.content_type).toBe('image/png');
    expect(body.document_ref.content_type).toBe('application/pdf');
    // Ni clip ni comprobación automática en el cuerpo (D-2).
    expect(Object.keys(body).sort()).toEqual([
      'blur_verification',
      'cameras',
      'document_ref',
      'scope_text_es',
    ]);
    // La compuerta de montaje queda aprobada con la respuesta y el chip lo refleja.
    await waitFor(() => {
      expect(chips(container)).toEqual(['no_use_agreement']);
    }, WAIT);
  });

  it('sin declaración ni captura el servidor responde catalog_blur_not_verified, anclado al acta', async () => {
    serveReads({ gates: () => gateState('pending'), policy: plantPolicy(false) });
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/gates/mounting/scope-record`, async ({ request }) => {
        bodies.push(await request.json());
        return apiError('conflict', 409, { detailCode: 'catalog_blur_not_verified' });
      }),
    );
    const { section } = await renderWizard();
    await fillTextAndCameras(section);
    // El paso del difuminado se deja sin declarar ni adjuntar: lo decide el servidor.
    await within(section).findByRole('checkbox', { name: text('scr05.scope.blur_declared') }, WAIT);
    fireEvent.click(next(section));
    await within(section).findByLabelText(
      text('scr05.scope.document'),
      { selector: 'input' },
      WAIT,
    );
    fireEvent.click(next(section));
    fireEvent.click(
      await within(section).findByRole(
        'button',
        { name: text('components_structure.wizard.submit') },
        WAIT,
      ),
    );
    const alert = await waitFor(() => {
      const node = section.querySelector('[role="alert"][data-code="conflict"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    expect(alert.textContent).not.toBe('');
    expect(bodies).toEqual([
      {
        scope_text_es: SCOPE,
        cameras: [{ camera_id: CAMERA, framing_description_es: FRAMING, reference_marker: true }],
        blur_verification: { declared: false, capture_document_ref: null },
        document_ref: null,
      },
    ]);
    expect(screen.queryByText(text('scr05.scope.done'))).toBeNull();
  });

  it('sin cámaras en el catálogo no se puede avanzar del paso de encuadres', async () => {
    serveReads({ gates: () => gateState('pending'), cameras: [] });
    const { section } = await renderWizard();
    fireEvent.change(within(section).getByLabelText(text('scr05.scope.text')), {
      target: { value: SCOPE },
    });
    await waitFor(() => {
      expect(next(section).hasAttribute('disabled')).toBe(false);
    }, WAIT);
    fireEvent.click(next(section));
    await within(section).findByText(text('scr05.scope.no_cameras'), {}, WAIT);
    expect(next(section).hasAttribute('disabled')).toBe(true);
  });
});
