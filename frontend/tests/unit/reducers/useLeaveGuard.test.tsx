/**
 * `useLeaveGuard` (BR-APP-20): con `dirty` pide confirmación antes de salir de la pantalla y
 * ante `beforeunload`; sin cambios no la pide. Enrutador en memoria en modo de datos.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { createMemoryRouter, Link, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { useLeaveGuard } from '@/state/reducers/useLeaveGuard';

/** Tope de espera holgado: no es lo que se prueba (AGENTS.md, nada de topes de 1 s). */
const WAIT = { timeout: 10_000 };

function Form() {
  const [dirty, setDirty] = useState(false);
  const guard = useLeaveGuard(dirty);
  return (
    <main>
      <h1>Formulario</h1>
      <button
        type="button"
        onClick={() => {
          setDirty(true);
        }}
      >
        Escribir
      </button>
      <Link to="/other">Salir</Link>
      <Link to="/form?tab=2">Otra pestaña</Link>
      {guard.confirming && (
        <div role="alertdialog" aria-label="Cambios sin guardar">
          <button type="button" onClick={guard.stay}>
            Quedarme
          </button>
          <button type="button" onClick={guard.leave}>
            Salir sin guardar
          </button>
        </div>
      )}
    </main>
  );
}

function renderForm() {
  const router = createMemoryRouter(
    [
      { path: '/form', element: <Form /> },
      { path: '/other', element: <h1>Otra pantalla</h1> },
    ],
    { initialEntries: ['/form'] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

function unload(): Event {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event;
}

describe('useLeaveGuard', { timeout: 30_000 }, () => {
  it('sin cambios no pide confirmación al salir ni al recargar', async () => {
    const router = renderForm();
    expect(unload().defaultPrevented).toBe(false);
    fireEvent.click(screen.getByRole('link', { name: 'Salir' }));
    expect(await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT)).toBeTruthy();
    expect(router.state.location.pathname).toBe('/other');
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('con cambios pide confirmación; quedarse conserva la pantalla y salir navega', async () => {
    const router = renderForm();
    fireEvent.click(screen.getByRole('button', { name: 'Escribir' }));
    expect(unload().defaultPrevented).toBe(true);

    fireEvent.click(screen.getByRole('link', { name: 'Salir' }));
    expect(await screen.findByRole('alertdialog', {}, WAIT)).toBeTruthy();
    expect(router.state.location.pathname).toBe('/form');

    fireEvent.click(screen.getByRole('button', { name: 'Quedarme' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(router.state.location.pathname).toBe('/form');

    fireEvent.click(screen.getByRole('link', { name: 'Salir' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Salir sin guardar' }, WAIT));
    expect(await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT)).toBeTruthy();
    expect(router.state.location.pathname).toBe('/other');
  });

  it('con cambios, navegar dentro de la misma pantalla no pide confirmación', async () => {
    const router = renderForm();
    fireEvent.click(screen.getByRole('button', { name: 'Escribir' }));
    fireEvent.click(screen.getByRole('link', { name: 'Otra pestaña' }));
    await waitFor(() => {
      expect(router.state.location.search).toBe('?tab=2');
    }, WAIT);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('al desmontar quita el aviso de beforeunload', async () => {
    const router = renderForm();
    fireEvent.click(screen.getByRole('button', { name: 'Escribir' }));
    fireEvent.click(screen.getByRole('link', { name: 'Salir' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Salir sin guardar' }, WAIT));
    await screen.findByRole('heading', { name: 'Otra pantalla' }, WAIT);
    expect(router.state.location.pathname).toBe('/other');
    expect(unload().defaultPrevented).toBe(false);
  });
});
