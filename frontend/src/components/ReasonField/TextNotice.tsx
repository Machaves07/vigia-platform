/**
 * El motivo real de un rechazo del servidor sobre un texto libre (BR-APP-43, 78; A-68, A-73, A-126),
 * anclado al campo o al formulario que lo envió:
 *
 * - un rechazo de los validadores de U-04 (`loop_blocked_vocabulary_term`,
 *   `loop_person_attribution_detected`) muestra el código, el término y la sugerencia tal cual
 *   llegan (`ValidatorRejection`, el mismo de SCR-11). En una atribución a persona la sugerencia
 *   del patrón es el mensaje principal y, si el texto tiene la forma de `proper_name` (dos palabras
 *   seguidas con mayúscula inicial o en mayúsculas), se añade «pruebe en minúsculas» (A-126, Q14).
 *   El servidor no dice qué patrón cayó (P3): la pista sale de la forma de lo escrito;
 * - en un texto del lazo, un 400 `invalid_request` sin `detail_code` es la política base: dice qué
 *   revisar (longitud, saltos de línea o caracteres) con el `correlation_id`, nunca el genérico
 *   (A-126, Q13). Con el espejo local, desde la consola no debería ocurrir.
 */
import { ValidatorRejection } from '@/components/AssistantDraftPanel/AssistantDraftPanel';
import { rejectionOf } from '@/components/AssistantDraftPanel/state';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { text } from '@/texts';

import styles from './ReasonField.module.css';

const REVIEW_KEY = 'components_forms.reason.review';
const LOWERCASE_HINT_KEY = 'components_forms.reason.lowercase_hint';

/** `detail_code` de los rechazos de un texto libre del lazo (los de U-04 que nombran el texto). */
const TEXT_DETAIL_CODES = [
  'loop_reason_required',
  'loop_reason_too_short',
  'loop_blocked_vocabulary_term',
  'loop_person_attribution_detected',
] as const;

/**
 * El campo al que se ancla un rechazo de texto del formulario: el único con texto escrito. Con
 * varios, el formulario: `ApiErrorBody` no dice cuál cayó y anclarlo a otro despistaría.
 */
export function textFields(
  texts: Readonly<Record<string, string | null>>,
): Readonly<Record<string, string>> {
  const written = Object.keys(texts).filter((field) => (texts[field] ?? '').trim() !== '');
  const field = written.length === 1 ? (written[0] ?? 'form') : 'form';
  return Object.fromEntries(TEXT_DETAIL_CODES.map((code) => [code, field]));
}

/** Un 400 de la política base: `invalid_request` sin `detail_code` (A-126, Q13). */
function untyped(notice: Notice): boolean {
  return notice.error.code === 'invalid_request' && !notice.error.detail_code;
}

/** El aviso es el rechazo de un texto: de un validador de U-04 o de la política base. */
export function textRejection(notice: Notice): boolean {
  return (
    untyped(notice) ||
    rejectionOf(notice.error.body, notice.error.detail_code ?? null).code !== null
  );
}

function nameShaped(word: string): boolean {
  const chars = Array.from(word);
  if (chars.length < 2) return false;
  if (word === word.toUpperCase() && word !== word.toLowerCase()) return true;
  const [first = '', ...rest] = chars;
  const tail = rest.join('');
  return (
    first === first.toUpperCase() &&
    first !== first.toLowerCase() &&
    tail === tail.toLowerCase() &&
    tail !== tail.toUpperCase()
  );
}

/** Dos palabras seguidas por un espacio, con mayúscula inicial o en mayúsculas (`proper_name`). */
function nameShapedPair(written: string): boolean {
  const cased = written.normalize('NFKD').replace(/\p{M}/gu, '');
  let previous: RegExpExecArray | null = null;
  for (const found of cased.matchAll(/\p{L}+/gu)) {
    if (
      previous !== null &&
      cased.slice(previous.index + previous[0].length, found.index) === ' ' &&
      nameShaped(previous[0]) &&
      nameShaped(found[0])
    ) {
      return true;
    }
    previous = found;
  }
  return false;
}

export function TextNotice({
  id,
  notice,
  written,
  loopText = false,
}: {
  readonly id: string;
  readonly notice: Notice;
  /** Lo que la persona escribió (para la pista de las mayúsculas); nunca se envía. */
  readonly written: string;
  readonly loopText?: boolean;
}) {
  if (loopText && untyped(notice)) {
    return <FieldNotice id={id} notice={notice} textKey={REVIEW_KEY} />;
  }
  const rejection = rejectionOf(notice.error.body, notice.error.detail_code ?? null);
  if (rejection.code === null) return <FieldNotice id={id} notice={notice} />;
  const attribution = rejection.code === 'person_attribution_detected';
  return (
    <FieldNotice
      id={id}
      notice={notice}
      message={attribution ? (rejection.suggestion ?? undefined) : undefined}
    >
      <ValidatorRejection rejection={rejection} />
      {attribution && nameShapedPair(written) ? (
        <p className={styles.hint} data-part="lowercase_hint" data-text-key={LOWERCASE_HINT_KEY}>
          {text(LOWERCASE_HINT_KEY)}
        </p>
      ) : null}
    </FieldNotice>
  );
}
