/**
 * SCR-11, el análisis firmado muestra la cadena de porqués (O-10, TASK-584): los porqués guardados
 * en orden, cada uno con su categoría etiquetada o sin ella, y después la categoría de la causa
 * raíz, el enunciado y la conclusión. Con MSW (el servidor falso de U-04 de `support.tsx`): se
 * retoma un análisis guardado, se firma y se lee la vista firmada que devuelve el servidor.
 * Comprobaciones por `data-part`, clave de texto y etiqueta de la unidad dueña, por igualdad.
 * Va en su propio archivo para no chocar con los cambios en curso de `root_cause.test.tsx`.
 */
import { fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import Page from '@/pages/scr11/root_cause';
import { text } from '@/texts';
import { label, labelInline } from '@/texts/labels';

import { analysis, fakeLoop, providers, question, RECORD, renderAt, WAIT } from './support';

const WHYS = [
  {
    statement: 'Primer porqué sintético: la guarda se abrió con la máquina en marcha.',
    category: 'process',
  },
  { statement: 'Segundo porqué sintético: el enclavamiento no estaba conectado.', category: null },
  {
    statement: 'Tercer porqué sintético: el montaje no incluyó la prueba del enclavamiento.',
    category: 'engineering',
  },
] as const;

const ROOT = {
  category: 'engineering',
  statement: 'Enunciado sintético de la causa raíz de ingeniería.',
  conclusion: 'Conclusión sintética del análisis de la zona.',
};

function saved() {
  return analysis({
    why_steps: WHYS.map((why, offset) => ({
      step_index: offset + 1,
      question_es: question(offset + 1),
      statement: why.statement,
      evidence_refs: [],
      category: why.category,
    })),
    next_step_index: 4,
    next_question_es: question(4),
    pending_information: ['root_cause_category', 'root_cause_statement', 'conclusion'],
  });
}

function next() {
  fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
}

/** Retoma el análisis guardado, completa la causa raíz, firma y devuelve la vista firmada. */
async function signedView() {
  const env = await providers();
  const loop = fakeLoop();
  loop.saved(saved());
  loop.install(env.clock);
  const view = renderAt(<Page />, env.Providers);
  fireEvent.click(await screen.findByRole('button', { name: text('scr11.continue') }, WAIT));
  await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
  for (let index = 1; index <= 5; index += 1) next();
  fireEvent.change(screen.getByRole('combobox', { name: text('scr11.root_category') }), {
    target: { value: ROOT.category },
  });
  fireEvent.change(screen.getByRole('textbox', { name: text('scr11.root_statement') }), {
    target: { value: ROOT.statement },
  });
  fireEvent.change(screen.getByRole('textbox', { name: text('scr11.conclusion') }), {
    target: { value: ROOT.conclusion },
  });
  next();
  const block = await screen.findByRole(
    'region',
    { name: text('components_forms.signature.title') },
    WAIT,
  );
  fireEvent.click(
    within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
  );
  fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.submit') }));
  const signed = await screen.findByRole('region', { name: text('scr11.signed_title') }, WAIT);
  return { loop, view, signed };
}

function part(root: Element, name: string): Element | null {
  return root.querySelector(`[data-part="${name}"]`);
}

describe('SCR-11 análisis firmado con su cadena de porqués (TASK-584)', { timeout: 60_000 }, () => {
  it('lista los porqués guardados en orden, cada uno con su categoría etiquetada o sin ella', async () => {
    const { loop, signed } = await signedView();
    expect(loop.calls.signatures).toHaveLength(1);
    const whys = [...signed.querySelectorAll('[data-part="summary-whys"] [data-part="why"]')];
    expect(whys.map((why) => part(why, 'why-statement')?.textContent)).toEqual(
      WHYS.map((why) => why.statement),
    );
    expect(
      whys.map((why) => {
        const category = part(why, 'why-category');
        return category === null
          ? null
          : [category.getAttribute('data-value'), category.textContent];
      }),
    ).toEqual([
      ['process', labelInline('cause_category', 'process')],
      null,
      ['engineering', labelInline('cause_category', 'engineering')],
    ]);
  });

  it('orden: quién firmó, los porqués y después la categoría, el enunciado y la conclusión', async () => {
    const { signed } = await signedView();
    expect(
      [...signed.children].map((child) => child.getAttribute('data-part') ?? child.tagName),
    ).toEqual(['H2', 'P', 'signed-facts', 'H3', 'summary-whys', 'H3', 'signed-root']);
    const headings = within(signed).getAllByRole('heading', { level: 3 });
    expect(headings.map((heading) => heading.textContent)).toEqual([
      text('scr11.summary_whys'),
      text('scr11.summary_root'),
    ]);
    expect([
      part(signed, 'record')?.textContent,
      part(signed, 'root-category')?.textContent,
      part(signed, 'root-statement')?.textContent,
      part(signed, 'conclusion')?.textContent,
    ]).toEqual([RECORD, label('cause_category', ROOT.category), ROOT.statement, ROOT.conclusion]);
    // Solo lectura: ningún botón de editar ni de borrar en la vista firmada (BR-APP-24).
    expect(within(signed).queryAllByRole('button')).toHaveLength(0);
  });

  it('axe sin violaciones con el análisis firmado y sus porqués', async () => {
    const { view } = await signedView();
    expect(await axe(view.container)).toHaveNoViolations();
  });
});
