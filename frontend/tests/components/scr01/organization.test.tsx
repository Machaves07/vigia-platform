import { act, fireEvent, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr01/organization';
import { text } from '@/texts';
import { behaviourFor } from '@/ports/errors/map';
import { server } from '../../msw/server';
import { apiError, renderAdmin, settings, WAIT } from './hierarchySupport';

const maximum = () =>
  screen.getByRole('spinbutton', { name: text('scr01_hierarchy.concession_max_days') });
const defaultDays = () =>
  screen.getByRole('spinbutton', { name: text('scr01_hierarchy.concession_default_days') });
const submit = () => {
  fireEvent.submit(screen.getByRole('form', { name: text('scr01_hierarchy.concession_settings') }));
};
async function ready() {
  const view = await renderAdmin(<Page />);
  await screen.findByRole(
    'spinbutton',
    { name: text('scr01_hierarchy.concession_max_days') },
    WAIT,
  );
  return view;
}

describe('SCR-01 ajustes de concesión', { timeout: 60_000 }, () => {
  it('lee ajustes y presenta límites del esquema generado', async () => {
    await ready();
    expect((maximum() as HTMLInputElement).value).toBe('30');
    expect((defaultDays() as HTMLInputElement).value).toBe('7');
    expect(maximum().getAttribute('min')).toBe('1');
    expect(maximum().getAttribute('max')).toBe('90');
  });

  it.each(['0', '91', '1.5', ''])(
    'no envía el valor estructuralmente inválido %j y ancla el error al campo',
    async (value) => {
      await ready();
      let writes = 0;
      server.use(
        http.patch('http://localhost/organization/settings', () => {
          writes += 1;
          return HttpResponse.json(settings());
        }),
      );
      fireEvent.change(maximum(), { target: { value } });
      submit();
      await act(async () => {
        await Promise.resolve();
      });
      expect(maximum().getAttribute('aria-invalid')).toBe('true');
      const error = document.getElementById(maximum().getAttribute('aria-describedby') ?? '');
      expect(error?.getAttribute('data-text-key')).toBe(
        value === '1.5' || value === '' ? 'validation.type' : 'validation.out_of_range',
      );
      expect(writes).toBe(0);
    },
  );

  it.each([1, 90])('envía el borde %i y renueva la lectura después de guardar', async (value) => {
    await ready();
    const bodies: unknown[] = [];
    let reads = 0;
    server.use(
      http.patch('http://localhost/organization/settings', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          ...settings(),
          concession_max_days: value,
          concession_default_days: value,
        });
      }),
      http.get('http://localhost/organization/settings', () => {
        reads += 1;
        return HttpResponse.json(settings());
      }),
    );
    fireEvent.change(maximum(), { target: { value: String(value) } });
    fireEvent.change(defaultDays(), { target: { value: String(value) } });
    submit();
    await screen.findByText(text('scr01_hierarchy.saved'), {}, WAIT);
    expect(bodies).toEqual([{ concession_max_days: value, concession_default_days: value }]);
    expect(reads).toBe(1);
  });

  it('no replica la relación máximo/por defecto: la decide el servidor y su mensaje prevalece', async () => {
    await ready();
    let writes = 0;
    server.use(
      http.patch('http://localhost/organization/settings', () => {
        writes += 1;
        return apiError('invalid_request', 400);
      }),
    );
    fireEvent.change(maximum(), { target: { value: '1' } });
    fireEvent.change(defaultDays(), { target: { value: '90' } });
    submit();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      behaviourFor({
        code: 'invalid_request',
        status: 400,
        message_es: 'Mensaje sintético del servidor.',
      }).message_es,
    );
    expect((defaultDays() as HTMLInputElement).value).toBe('90');
    expect(writes).toBe(1);
    const form = screen.getByRole('form', { name: text('scr01_hierarchy.concession_settings') });
    expect(
      document.getElementById(form.getAttribute('aria-describedby') ?? '')?.contains(alert),
    ).toBe(true);
    expect(screen.getByRole('button', { name: text('scr01_hierarchy.save') })).toHaveProperty(
      'disabled',
      false,
    );
  });

  it('axe sin violaciones serias en los ajustes', async () => {
    const { container } = await ready();
    const result = await axe(container);
    expect(
      result.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
    ).toEqual([]);
  });
});
