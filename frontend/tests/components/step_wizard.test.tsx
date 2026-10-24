/**
 * UI-15 `StepWizard` (BR-APP-45, 20): un paso por etapa con el avance bloqueado hasta responder,
 * resumen antes de enviar, «Guardar sin terminar» solo con `server_progress_ref` y confirmación
 * al abandonar con cambios (`useLeaveGuard`, enrutador en memoria en modo de datos). `axe` sin
 * violaciones serias. Se comparan textos del catálogo y nombres accesibles exactos.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { createMemoryRouter, Link, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { StepWizard } from '@/components/StepWizard/StepWizard';
import catalog from '@/texts/components_structure.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

/** Tope de espera holgado: no es lo que se prueba. */
const WAIT = { timeout: 10_000 };

function Screen({
  serverProgressRef = null,
  onSubmit,
  onSavePartial,
}: {
  serverProgressRef?: string | null;
  onSubmit: () => Promise<void>;
  onSavePartial: () => Promise<void>;
}) {
  const [why, setWhy] = useState('');
  const [category, setCategory] = useState('');
  const dirty = why !== '' || category !== '';
  return (
    <main>
      <Link to="/other">Salir</Link>
      <StepWizard
        title="Causa raíz"
        dirty={dirty}
        serverProgressRef={serverProgressRef}
        steps={[
          {
            id: 'why',
            title: 'Primer porqué',
            complete: why.trim() !== '',
            content: (
              <label>
                Porqué
                <input
                  value={why}
                  onChange={(event) => {
                    setWhy(event.target.value);
                  }}
                />
              </label>
            ),
          },
          {
            id: 'category',
            title: 'Categoría',
            complete: category !== '',
            content: (
              <label>
                Categoría
                <select
                  value={category}
                  onChange={(event) => {
                    setCategory(event.target.value);
                  }}
                >
                  <option value="">Elija</option>
                  <option value="process">Proceso</option>
                </select>
              </label>
            ),
          },
        ]}
        summary={<p data-testid="summary">{`${why} / ${category}`}</p>}
        onSubmit={onSubmit}
        onSavePartial={onSavePartial}
      />
    </main>
  );
}

function renderWizard({
  serverProgressRef = null,
  failSubmit = false,
}: { serverProgressRef?: string | null; failSubmit?: boolean } = {}) {
  const submit = vi.fn(() => (failSubmit ? Promise.reject(new Error('503')) : Promise.resolve()));
  const save = vi.fn(() => Promise.resolve());
  const router = createMemoryRouter(
    [
      {
        path: '/wizard',
        element: (
          <Screen serverProgressRef={serverProgressRef} onSubmit={submit} onSavePartial={save} />
        ),
      },
      { path: '/other', element: <h1>Otra pantalla</h1> },
    ],
    { initialEntries: ['/wizard'] },
  );
  const result = render(<RouterProvider router={router} />);
  const calls = {
    get submits() {
      return submit.mock.calls.length;
    },
    get saves() {
      return save.mock.calls.length;
    },
  };
  return { ...result, router, calls };
}

const button = (key: CatalogKey) => screen.getByRole('button', { name: t(key) });
const stepTitle = () => document.querySelector('[data-part="step-title"]')?.textContent;

describe('UI-15 StepWizard', { timeout: 30_000 }, () => {
  it('bloquea el avance hasta responder y muestra el resumen antes de enviar', async () => {
    const user = userEvent.setup();
    const { calls } = renderWizard();
    expect(stepTitle()).toBe(
      `${t('components_structure.wizard.step')} 1 ${t('components_structure.wizard.of')} 2: Primer porqué`,
    );
    expect((button('components_structure.wizard.next') as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.queryByRole('button', { name: t('components_structure.wizard.submit') }),
    ).toBeNull();

    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'Falta guarda');
    await waitFor(() => {
      expect((button('components_structure.wizard.next') as HTMLButtonElement).disabled).toBe(
        false,
      );
    }, WAIT);
    await user.click(button('components_structure.wizard.next'));
    expect(stepTitle()).toBe(
      `${t('components_structure.wizard.step')} 2 ${t('components_structure.wizard.of')} 2: Categoría`,
    );
    expect((button('components_structure.wizard.next') as HTMLButtonElement).disabled).toBe(true);

    await user.selectOptions(screen.getByRole('combobox', { name: 'Categoría' }), 'process');
    await user.click(button('components_structure.wizard.next'));
    expect(stepTitle()).toBe(t('components_structure.wizard.summary'));
    expect(screen.getByTestId('summary').textContent).toBe('Falta guarda / process');
    expect(calls.submits).toBe(0);

    await user.click(button('components_structure.wizard.submit'));
    expect((await screen.findByRole('status', {}, WAIT)).textContent).toBe(
      t('components_structure.wizard.submitted'),
    );
    expect(calls.submits).toBe(1);
  });

  it('volver a un paso y dejarlo sin responder vuelve a bloquear el avance', async () => {
    const user = userEvent.setup();
    renderWizard();
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(button('components_structure.wizard.next'));
    await user.click(button('components_structure.wizard.back'));
    await user.clear(screen.getByRole('textbox', { name: 'Porqué' }));
    await waitFor(() => {
      expect((button('components_structure.wizard.next') as HTMLButtonElement).disabled).toBe(true);
    }, WAIT);
    expect(screen.queryByRole('button', { name: 'Categoría' })).toBeNull();
  });

  it('sin server_progress_ref no existe «Guardar sin terminar»', async () => {
    const user = userEvent.setup();
    renderWizard({ serverProgressRef: null });
    expect(
      screen.queryByRole('button', { name: t('components_structure.wizard.save_partial') }),
    ).toBeNull();
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(button('components_structure.wizard.next'));
    expect(
      screen.queryByRole('button', { name: t('components_structure.wizard.save_partial') }),
    ).toBeNull();
  });

  it('con server_progress_ref «Guardar sin terminar» guarda en el servidor', async () => {
    const user = userEvent.setup();
    const { calls } = renderWizard({ serverProgressRef: 'rca-0001' });
    await user.click(button('components_structure.wizard.save_partial'));
    expect((await screen.findByRole('status', {}, WAIT)).textContent).toBe(
      t('components_structure.wizard.saved_partial'),
    );
    expect(calls.saves).toBe(1);
  });

  it('un envío fallido avisa y deja reintentar', async () => {
    const user = userEvent.setup();
    const { calls } = renderWizard({ failSubmit: true });
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(button('components_structure.wizard.next'));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Categoría' }), 'process');
    await user.click(button('components_structure.wizard.next'));
    await user.click(button('components_structure.wizard.submit'));
    expect((await screen.findByRole('alert', {}, WAIT)).textContent).toBe(
      t('components_structure.wizard.submit_failed'),
    );
    expect((button('components_structure.wizard.submit') as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(calls.submits).toBe(1);
  });

  it('sin cambios, salir no pide confirmación', async () => {
    const user = userEvent.setup();
    const { router } = renderWizard();
    await user.click(screen.getByRole('link', { name: 'Salir' }));
    expect(await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT)).toBeTruthy();
    expect(router.state.location.pathname).toBe('/other');
  });

  it('con cambios, salir pide confirmación; quedarse conserva el paso y salir navega', async () => {
    const user = userEvent.setup();
    const { router } = renderWizard();
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(screen.getByRole('link', { name: 'Salir' }));
    const dialog = await screen.findByRole(
      'alertdialog',
      { name: t('components_structure.wizard.leave_title') },
      WAIT,
    );
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: t('components_structure.wizard.stay') }),
    );
    expect(dialog.getAttribute('aria-describedby')).not.toBeNull();
    expect(router.state.location.pathname).toBe('/wizard');

    await user.click(button('components_structure.wizard.stay'));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Porqué' }).value).toBe('x');

    await user.click(screen.getByRole('link', { name: 'Salir' }));
    await user.click(
      await screen.findByRole('button', { name: t('components_structure.wizard.leave') }, WAIT),
    );
    expect(await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT)).toBeTruthy();
  });

  it('tras enviar, salir ya no pide confirmación', async () => {
    const user = userEvent.setup();
    const { router } = renderWizard();
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(button('components_structure.wizard.next'));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Categoría' }), 'process');
    await user.click(button('components_structure.wizard.next'));
    await user.click(button('components_structure.wizard.submit'));
    await screen.findByRole('status', {}, WAIT);
    await user.click(screen.getByRole('link', { name: 'Salir' }));
    expect(await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT)).toBeTruthy();
    expect(router.state.location.pathname).toBe('/other');
  });

  it('axe sin violaciones en un paso, con la confirmación abierta y en el resumen', async () => {
    const user = userEvent.setup();
    const { container } = renderWizard({ serverProgressRef: 'rca-0001' });
    const check = async () => {
      const results = await axe(container);
      const serious = results.violations.filter(
        (violation) => violation.impact === 'serious' || violation.impact === 'critical',
      );
      expect(serious).toEqual([]);
      expect(results).toHaveNoViolations();
    };
    await check();
    await user.type(screen.getByRole('textbox', { name: 'Porqué' }), 'x');
    await user.click(screen.getByRole('link', { name: 'Salir' }));
    await screen.findByRole('alertdialog', {}, WAIT);
    await check();
    await user.click(button('components_structure.wizard.stay'));
    await user.click(button('components_structure.wizard.next'));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Categoría' }), 'process');
    await user.click(button('components_structure.wizard.next'));
    await check();
  });
});
