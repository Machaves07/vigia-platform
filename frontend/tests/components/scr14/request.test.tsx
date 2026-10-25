/** IF-17: bordes de solicitud y rechazos anclados sin perder los valores. */
import { act, fireEvent, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import type { ExportRequest } from '@/ports/scr14Exports';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { API, EXPORT, WAIT, fillRequest, fixture, job } from './support';

describe('SCR-14 solicitud', { timeout: 30_000 }, () => {
  it.each([
    [21, '2026-01-02T00:00'],
    [1, '2027-01-03T00:00'],
    [0, '2026-01-02T00:00'],
    [1, '2026-01-01T00:00'],
    [1, '2025-12-31T00:00'],
  ])('no envía %i zonas hasta %s', async (count, to) => {
    await fixture();
    let calls = 0;
    server.use(
      http.post(`${API}/accreditation/exports`, () => {
        calls++;
        return HttpResponse.json(job('requested'), { status: 202 });
      }),
    );
    fillRequest(count, to);
    fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
    expect(screen.getByRole('alert').textContent).toBe(text('scr14.invalid'));
    expect(calls).toBe(0);
  });

  it.each(['with_clips', 'without_clips'] as const)(
    'envía 20 zonas y 366 días con %s en hora de planta; axe',
    async (packageForm) => {
      const { view } = await fixture();
      let sent: ExportRequest | null = null;
      server.use(
        http.post(`${API}/accreditation/exports`, async ({ request }) => {
          sent = (await request.json()) as ExportRequest;
          return HttpResponse.json(job('requested'), { status: 202 });
        }),
      );
      fillRequest(20, '2027-01-02T00:00');
      fireEvent.change(screen.getByLabelText(text('scr14.package_form')), {
        target: { value: packageForm },
      });
      fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
      await screen.findByRole('region', { name: text('scr14.manifest') }, WAIT);
      expect(sent).toMatchObject({
        period: { from: '2026-01-01T05:00:00.000Z', to: '2027-01-02T05:00:00.000Z' },
        package_form: packageForm,
      });
      expect((sent as unknown as ExportRequest).zone_ids).toHaveLength(20);
      expect(
        within(screen.getByRole('region', { name: text('scr14.jobs') })).getByRole('button', {
          name: EXPORT,
        }).textContent,
      ).toBe(EXPORT);
      expect(await axe(view.container)).toHaveNoViolations();
    },
  );

  it('advierte el acuerdo revocado sin excluir la zona ni ocultar la solicitud', async () => {
    await fixture();
    fillRequest();
    expect((await screen.findByRole('note', {}, WAIT)).textContent).toBe(text('scr14.revoked'));
    expect(
      screen.getByRole('checkbox', { name: 'Zona sintética 1' }).getAttribute('disabled'),
    ).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
    await screen.findByRole('region', { name: text('scr14.manifest') }, WAIT);
  });

  it.each([
    'accreditation_export_zones_span_plants',
    'accreditation_export_zone_out_of_scope',
    'accreditation_export_scope_too_large',
  ])('ancla %s y conserva selección', async (detail) => {
    await fixture();
    server.use(
      http.post(`${API}/accreditation/exports`, () =>
        HttpResponse.json(
          {
            code: 'invalid_request',
            detail_code: detail,
            message_es: 'Rechazo sintético',
            correlation_id: EXPORT,
          },
          { status: 400 },
        ),
      ),
    );
    fillRequest();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('invalid_request');
    expect(alert.parentElement?.getAttribute('aria-describedby')).toBe(alert.id);
    expect(screen.getByLabelText<HTMLInputElement>(text('scr14.from')).value).toBe(
      '2026-01-01T00:00',
    );
    expect(
      screen.getByRole('option', { name: label('package_form', 'without_clips') }),
    ).toBeDefined();
  });

  it('429 conserva el formulario y ofrece Repetir tras la cuenta regresiva', async () => {
    const { clock } = await fixture();
    server.use(
      http.post(`${API}/accreditation/exports`, () =>
        HttpResponse.json(
          {
            code: 'rate_limited',
            retry_after_seconds: 30,
            message_es: 'Límite sintético',
            correlation_id: EXPORT,
          },
          { status: 429 },
        ),
      ),
    );
    fillRequest();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
    const retry = await screen.findByRole(
      'button',
      { name: text('components_forms.notice.retry') },
      WAIT,
    );
    expect((retry as HTMLButtonElement).disabled).toBe(true);
    act(() => {
      clock.advance(29_000);
    });
    expect((retry as HTMLButtonElement).disabled).toBe(true);
    act(() => {
      clock.advance(1_000);
    });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    expect(
      within(screen.getByRole('form', { name: text('scr14.request') })).getByLabelText(
        text('scr14.from'),
      ),
    ).toBeDefined();
  });
});
