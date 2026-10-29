import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { changeDateTime } from '../../support/dateFields';
import {
  ACTION,
  ATTACHMENT,
  DESCRIPTION,
  INSTANT,
  WAIT,
  load,
  reasonName,
  renderClosure,
} from './closure_support';

async function fill(user: Awaited<ReturnType<typeof load>>) {
  await user.type(
    screen.getByRole('textbox', { name: reasonName('scr09.closure.description') }),
    DESCRIPTION,
  );
  changeDateTime(screen.getByLabelText(text('scr09.closure.performed_at')), '2026-10-24T10:00');
  await user.type(screen.getByLabelText(text('scr09.closure.performed_by_role')), 'Mantenimiento');
}
function uploadFile() {
  return new File(['abc'], 'resguardo.png', { type: 'image/png' });
}

describe('SCR-09 evidencia', { timeout: 40_000 }, () => {
  it('sin declaración no pide concesión; metadatos rechazados quedan anclados al adjunto', async () => {
    let grants = 0;
    await renderClosure();
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/actions/${ACTION}/attachments/upload-grant`, () => {
        grants++;
        return apiError('invalid_request', 400, {
          detailCode: 'loop_attachment_metadata_not_allowed',
        });
      }),
    );
    await user.upload(
      screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' }),
      uploadFile(),
    );
    await user.click(screen.getByRole('button', { name: text('media.upload.submit') }));
    await screen.findByText(text('media.upload.attachment_declaration_missing'), {}, WAIT);
    expect(grants).toBe(0);
    await user.click(screen.getByRole('checkbox', { name: text('media.upload.declaration') }));
    await user.click(screen.getByRole('button', { name: text('media.upload.retry') }));
    await screen.findByText(text('media.upload.attachment_metadata_not_allowed'), {}, WAIT);
    expect(grants).toBe(1);
    const field = screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' });
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(
      document
        .getElementById(field.getAttribute('aria-describedby')?.split(' ')[1] ?? '')
        ?.getAttribute('data-code'),
    ).toBe('attachment_metadata_not_allowed');
  });

  it('503 storage_unavailable sin detail_code degrada solo adjuntos y permite evidencia sin ellos', async () => {
    const { connection, container } = await renderClosure();
    const user = await load();
    const bodies: unknown[] = [];
    server.use(
      http.post(`http://localhost/loop/actions/${ACTION}/attachments/upload-grant`, () =>
        apiError('storage_unavailable', 503),
      ),
      http.post(`http://localhost/loop/actions/${ACTION}/evidence`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            ...(exampleFor('add_evidence_loop_actions__action_id__evidence_post') as object),
            added_at: INSTANT,
          },
          { status: 201 },
        );
      }),
    );
    await fill(user);
    await user.upload(
      screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' }),
      uploadFile(),
    );
    await user.click(screen.getByRole('checkbox', { name: text('media.upload.declaration') }));
    await user.click(screen.getByRole('button', { name: text('media.upload.submit') }));
    await screen.findByText(text('scr09.closure.storage_degraded'), {}, WAIT);
    for (let attempt = 1; attempt < 4; attempt++) {
      await user.click(screen.getByRole('button', { name: text('scr09.closure.retry_storage') }));
      await user.click(screen.getByRole('button', { name: text('media.upload.retry') }));
      await screen.findByText(text('scr09.closure.storage_degraded'), {}, WAIT);
      expect(connection.canRead('platform')).toBe(true);
    }
    expect(connection.canRead('platform')).toBe(true);
    await user.click(screen.getByRole('button', { name: text('scr09.closure.submit_evidence') }));
    await screen.findByText(text('scr09.closure.saved'), {}, WAIT);
    expect(bodies).toEqual([
      {
        description: DESCRIPTION,
        performed_at: '2026-10-24T15:00:00.000Z',
        performed_by_role: 'Mantenimiento',
        verification_note: null,
        attachments: [],
      },
    ]);
    expect(connection.canRead('platform')).toBe(true);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('compone cinco adjuntos y no permite una sexta concesión', async () => {
    let grants = 0;
    await renderClosure();
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/actions/${ACTION}/attachments/upload-grant`, () => {
        grants++;
        return HttpResponse.json(
          {
            attachment_id: ATTACHMENT.slice(0, -1) + String(grants),
            storage_key: 'sintetico',
            upload: {
              method: 'PUT',
              url: 'http://localhost/synthetic-store',
              required_headers: {
                'content-type': 'image/png',
                'x-amz-checksum-sha256': 'ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=',
              },
              expires_at: '2026-10-25T02:15:00.000Z',
            },
          },
          { status: 201 },
        );
      }),
      http.options(
        'http://localhost/synthetic-store',
        () =>
          new HttpResponse(null, {
            status: 200,
            headers: {
              'access-control-allow-origin': '*',
              'access-control-allow-methods': 'PUT',
              'access-control-allow-headers': 'content-type, x-amz-checksum-sha256',
            },
          }),
      ),
      http.put(
        'http://localhost/synthetic-store',
        () =>
          new HttpResponse(null, { status: 200, headers: { 'access-control-allow-origin': '*' } }),
      ),
    );
    for (let i = 0; i < 5; i++) {
      await user.upload(
        screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' }),
        uploadFile(),
      );
      await user.click(screen.getByRole('checkbox', { name: text('media.upload.declaration') }));
      await user.click(screen.getByRole('button', { name: text('media.upload.submit') }));
      await waitFor(() => {
        expect(
          within(screen.getByRole('form', { name: text('scr09.closure.evidence') })).getAllByText(
            'resguardo.png',
          ),
        ).toHaveLength(i + 1);
      }, WAIT);
    }
    await user.upload(
      screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' }),
      uploadFile(),
    );
    await user.click(screen.getByRole('checkbox', { name: text('media.upload.declaration') }));
    await user.click(screen.getByRole('button', { name: text('media.upload.submit') }));
    await waitFor(() => {
      expect(
        screen.getByRole<HTMLButtonElement>('button', {
          name: text('scr09.closure.submit_evidence'),
        }).disabled,
      ).toBe(false);
    }, WAIT);
    expect(
      screen.getByRole<HTMLButtonElement>('button', { name: text('media.upload.submit') }).disabled,
    ).toBe(true);
    expect(grants).toBe(5);
  });

  it('el rechazo de metadatos al registrar también se ancla al campo y conserva la evidencia', async () => {
    await renderClosure();
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/actions/${ACTION}/evidence`, () =>
        apiError('invalid_request', 400, { detailCode: 'loop_attachment_metadata_not_allowed' }),
      ),
    );
    await fill(user);
    await user.click(screen.getByRole('button', { name: text('scr09.closure.submit_evidence') }));
    await screen.findByText(text('media.upload.attachment_metadata_not_allowed'), {}, WAIT);
    const input = screen.getByLabelText(text('scr09.closure.attachments'), { selector: 'input' });
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(
      screen.getByRole<HTMLTextAreaElement>('textbox', {
        name: reasonName('scr09.closure.description'),
      }).value,
    ).toBe(DESCRIPTION);
  });
});
