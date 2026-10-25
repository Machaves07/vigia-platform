/** Preferencias reales GET/PUT: árbol cerrado, campos propios y límites de esquema. */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { notificationsPort } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { personFieldsDeclared, assertNoPersonData } from '../../support/viewProperties';
import views from '../../views/scr15';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';
import { WAIT, apiError, preferences, setup } from './support';

describe('SCR-15 preferencias', { timeout: 60_000 }, () => {
  it('RV-19 solo permite cuatro banderas de correo, nunca una dirección o identidad', () => {
    const registration = views.preferences;
    expect(personFieldsDeclared(registration)).toEqual([]);
    expect(
      personFieldsDeclared({
        ...registration,
        allowedFields: [
          'email',
          'email_address',
          'person_id',
          'worker_name',
          'email_enabled_immediate',
        ],
      }),
    ).toEqual(['email', 'email_address', 'person_id', 'worker_name', 'email_enabled_immediate']);
    expect(
      personFieldsDeclared({
        screen: 'SCR-08',
        view: 'RV-11',
        allowedFields: registration.allowedFields,
      }),
    ).toEqual([
      'email_enabled_normal',
      'email_enabled_digest',
      'email_suppressed_hard_bounce',
      'email_suppressed_complaint',
    ]);
  });

  it(
    'las dos vistas de producción rechazan campos ajenos inyectados',
    () => {
      for (const view of Object.values(views)) assertNoPersonData(view);
    },
    PROPERTY_TIMEOUT_MS,
  );
  it('rellena desde GET, sin inmediato ni controles de supresión en el árbol accesible', async () => {
    const { calls, view } = await setup({ page: 'preferences' });
    const form = await screen.findByRole('form', { name: text('scr15.preferences') }, WAIT);
    const checkboxes = within(form).getAllByRole('checkbox');
    expect(
      checkboxes.map((node) => ({
        name: (node as HTMLInputElement).labels?.[0]?.textContent,
        checked: (node as HTMLInputElement).checked,
      })),
    ).toEqual([
      { name: text('scr15.email_enabled_normal'), checked: false },
      { name: text('scr15.email_enabled_digest'), checked: true },
    ]);
    expect(within(form).getByRole('combobox', { name: text('scr15.weekday') })).toHaveProperty(
      'value',
      '7',
    );
    expect(
      within(form)
        .getAllByRole('option')
        .map((node) => (node as HTMLOptionElement).value),
    ).toEqual(['1', '2', '3', '4', '5', '6', '7']);
    expect(
      within(form)
        .getAllByRole('definition')
        .map((node) => node.textContent),
    ).toEqual([text('scr15.yes'), text('scr15.yes')]);
    expect(
      within(form)
        .getAllByRole('button')
        .map((node) => node.textContent),
    ).toEqual([text('scr15.save')]);
    expect(form.querySelectorAll('input,select,textarea')).toHaveLength(3);
    expect(calls.gets).toBe(1);
    expect(calls.saves).toEqual([]);
    expect(
      (await axe(view.container)).violations.filter(
        (v) => v.impact === 'serious' || v.impact === 'critical',
      ),
    ).toEqual([]);
  });

  it.each([1, 7])('guarda PUT con solo los tres campos permitidos y día %s', async (day) => {
    const { calls } = await setup({ page: 'preferences' });
    const normal = await screen.findByRole(
      'checkbox',
      { name: text('scr15.email_enabled_normal') },
      WAIT,
    );
    fireEvent.click(normal);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr15.weekday') }), {
      target: { value: String(day) },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr15.save') }));
    await screen.findByText(text('scr15.saved'), {}, WAIT);
    expect(calls.saves).toEqual([
      { email_enabled_normal: true, email_enabled_digest: true, weekly_digest_weekday: day },
    ]);
    expect(calls.gets).toBe(2);
  });

  it.each([0, 8, 1.5])('el puerto rechaza día fuera de esquema %s sin PUT', async (day) => {
    const { calls } = await setup({ page: 'preferences' });
    await screen.findByRole('form', { name: text('scr15.preferences') }, WAIT);
    await expect(
      notificationsPort.savePreferences.write({
        body: {
          email_enabled_normal: false,
          email_enabled_digest: true,
          weekly_digest_weekday: day,
        },
      }),
    ).rejects.toMatchObject({ failure: { kind: 'invalid_input' } });
    expect(calls.saves).toEqual([]);
  });

  it('no inventa preferencias si falla GET', async () => {
    await setup({ page: 'preferences', preferencesResponse: () => apiError() });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('form')).toBeNull();
  });

  it('400 de esquema: conserva borrador y no reintenta PUT ni inventa error de propiedad', async () => {
    const { calls } = await setup({
      page: 'preferences',
      saveResponse: () => apiError(400, 'invalid_request'),
    });
    fireEvent.click(
      await screen.findByRole('checkbox', { name: text('scr15.email_enabled_normal') }, WAIT),
    );
    fireEvent.click(screen.getByRole('button', { name: text('scr15.save') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(calls.saves).toHaveLength(1);
    expect(
      screen.getByRole('checkbox', { name: text('scr15.email_enabled_normal') }),
    ).toHaveProperty('checked', true);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: text('scr15.save') })).toHaveProperty(
        'disabled',
        false,
      );
    }, WAIT);
    expect(screen.queryByText(text('scr15.saved'))).toBeNull();
    expect(preferences().email_suppressed_complaint).toBe(true);
  });
});
