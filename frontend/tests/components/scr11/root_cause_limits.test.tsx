/**
 * SCR-11 (A-126, TASK-580): el contador y la validación del porqué salen de los límites de U-04
 * que da el puerto (`ROOT_CAUSE_TEXT_LIMITS`, comparados con el corpus en `root_cause.test.tsx`),
 * nunca de cifras escritas en la pantalla. Con unos límites sintéticos de 5 a 9, el contador y el
 * espejo cambian con ellos. Se comparan claves del catálogo y cuerpos por igualdad.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import Page from '@/pages/scr11/root_cause';
import { text } from '@/texts';

import { fakeLoop, providers, renderAt, WAIT } from './support';

vi.mock('@/ports/scr11RootCause', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ports/scr11RootCause')>();
  return {
    ...actual,
    ROOT_CAUSE_TEXT_LIMITS: {
      ...actual.ROOT_CAUSE_TEXT_LIMITS,
      why_statement: { minLength: 5, maxLength: 9 },
    },
  };
});

const NAME = `${text('scr11.statement')} 1 (${text('components_forms.reason.required')})`;

function statement(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: NAME });
}

function why(): HTMLElement {
  const part = document.querySelector<HTMLElement>('[data-part="why-1"]');
  if (part === null) throw new Error('falta el porqué 1');
  return part;
}

function localError(): string | null {
  return why().querySelector('[data-part="local_error"]')?.getAttribute('data-text-key') ?? null;
}

describe('SCR-11 límites del porqué con un esquema sintético de 5 a 9', { timeout: 60_000 }, () => {
  it('el contador y la validación siguen los límites del puerto, no unos fijos', async () => {
    const env = await providers();
    const loop = fakeLoop({ statement: { min: 5, max: 9 } });
    loop.install(env.clock);
    renderAt(<Page />, env.Providers);
    fireEvent.click(await screen.findByRole('button', { name: text('scr11.start') }, WAIT));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
    const save = () => {
      fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    };
    const counter = (length: number, minimum: number, maximum: number) =>
      `${String(length)} / ${String(maximum)} ${text('components_forms.reason.characters')} (${text('components_forms.reason.minimum')} ${String(minimum)})`;

    fireEvent.change(statement(), { target: { value: 'abcd' } });
    expect(why().querySelector('[data-part="counter"]')?.textContent).toBe(counter(4, 5, 9));
    save();
    await waitFor(() => {
      expect(localError()).toBe('validation.free_text.too_short');
    }, WAIT);

    fireEvent.change(statement(), { target: { value: 'abcdefghij' } });
    expect(localError()).toBe('validation.free_text.too_long');
    expect(loop.calls.steps).toEqual([]);

    // Siete caracteres: bajo el mínimo fijo de 20, válidos con los límites sintéticos.
    fireEvent.change(statement(), { target: { value: 'abcdefg' } });
    expect(localError()).toBeNull();
    save();
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps).toEqual([
      { index: 1, body: { statement: 'abcdefg', evidence_refs: [], category: null } },
    ]);
  });
});
