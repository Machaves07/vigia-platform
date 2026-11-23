/**
 * Los porqués de SCR-11 como pasos de UI-15 (TASK-581; BR-LAZ-61; BR-APP-45; PR-APP-13).
 *
 * - La barra muestra **los porqués guardados más el siguiente** (como mucho cinco) y después
 *   «Causa raíz y conclusión». Así nunca ofrece un porqué que el servidor rechazaría por
 *   `analysis_step_out_of_order` (solo admite el siguiente o uno ya guardado).
 * - Un porqué con texto solo está completo cuando **está guardado** y coincide con lo que hay en
 *   pantalla (texto recortado en NFC y categoría). El siguiente porqué, posterior al primero y en
 *   blanco, también está completo: cierra la cadena y lleva a la causa raíz.
 *
 * Funciones puras: la pantalla y la propiedad PR-APP-13 usan las mismas.
 */
import { submittedText } from '@/components/ReasonField/ReasonField';
import type { Analysis } from '@/ports/scr11RootCause';

/** Como mucho cinco porqués (`MAX_WHY_STEPS` de U-04). */
export const WHY_COUNT = 5;

export interface WhyDraft {
  readonly statement: string;
  /** `''`: sin categoría. */
  readonly category: string;
}

type SavedWhy = Analysis['why_steps'][number];

export function savedStep(analysis: Analysis, index: number): SavedWhy | undefined {
  return analysis.why_steps.find((step) => step.step_index === index);
}

function blank(value: string): boolean {
  return value.trim() === '';
}

/** Los porqués que muestra la barra: los guardados más el siguiente, de 1 a `WHY_COUNT`. */
export function visibleWhyCount(analysis: Analysis): number {
  return Math.min(analysis.why_steps.length + 1, WHY_COUNT);
}

/**
 * Un porqué posterior al primero puede quedar en blanco mientras no esté guardado: entonces no se
 * guarda. Uno ya guardado no se puede vaciar: en blanco es «obligatorio», anclado al campo.
 */
export function whyOptional(analysis: Analysis, offset: number): boolean {
  return offset > 0 && savedStep(analysis, offset + 1) === undefined;
}

/** El porqué en pantalla coincide con el guardado: lo que se enviaría, en NFC como el servidor. */
export function matches(saved: SavedWhy | undefined, draft: WhyDraft | undefined): boolean {
  if (saved === undefined || draft === undefined) return false;
  return (
    saved.statement.normalize('NFC') === submittedText(draft.statement).normalize('NFC') &&
    (saved.category ?? '') === draft.category
  );
}

export function synced(analysis: Analysis, drafts: readonly WhyDraft[], offset: number): boolean {
  return matches(savedStep(analysis, offset + 1), drafts[offset]);
}

/** Completo: guardado y sincronizado, o el siguiente porqué opcional en blanco. */
export function whyComplete(
  analysis: Analysis,
  drafts: readonly WhyDraft[],
  offset: number,
): boolean {
  const draft = drafts[offset];
  if (draft === undefined) return false;
  if (synced(analysis, drafts, offset)) return true;
  return whyOptional(analysis, offset) && blank(draft.statement);
}

/** Hay algo que guardar: texto o categoría distintos de lo guardado, o un porqué guardado vaciado. */
export function whyHasChanges(
  analysis: Analysis,
  drafts: readonly WhyDraft[],
  offset: number,
): boolean {
  const draft = drafts[offset];
  if (draft === undefined || synced(analysis, drafts, offset)) return false;
  return !blank(draft.statement) || savedStep(analysis, offset + 1) !== undefined;
}
