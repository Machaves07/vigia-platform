/**
 * Validador estructural del texto libre (BR-APP-42, PR-APP-12): espejo de la política base de
 * `backend/src/vigia_platform/ledger/free_text.py` (`apply_base_policy`, U-02).
 *
 * Mismo orden y mismos motivos que el servidor, para que el corpus de `u05-fixtures.json` (VIG-279)
 * dé el mismo veredicto por igualdad:
 *
 * 1. más de cuatro veces el máximo de caracteres antes de normalizar → `too_long`;
 * 2. normalización **NFC**: el texto que se enviaría es el normalizado;
 * 3. por carácter, en orden: control (`Cc`, `Zl`, `Zp`) → `control_character`; formato (`Cf`) →
 *    `invisible_character`; sustituto suelto, uso privado o sin asignar (`Cs`, `Co`, `Cn`) →
 *    `disallowed_character`; ignorable por defecto (rellenos hangul, CGJ, selectores de variación,
 *    braille en blanco) → `invisible_character`;
 * 4. marcado (etiquetas, comentarios, entidades con o sin `;`), también en la forma NFKC → `markup`;
 * 5. longitud en caracteres Unicode tras NFC: más que el máximo → `too_long`; menos que el mínimo →
 *    `too_short`.
 *
 * **Nunca es la barrera**: el servidor decide (y además aplica los validadores enchufados de U-04,
 * vocabulario bloqueado y atribución a personas, que aquí no se repiten). Diferencia conocida: las
 * categorías Unicode salen de la versión del navegador, que puede ser más nueva que la de Python
 * del servidor; un carácter asignado después puede pasar aquí y ser rechazado allí como
 * `disallowed_character`, y entonces manda el servidor.
 */

/** Motivo de rechazo de la política base (`FreeTextRejection` del servidor). */
export type FreeTextRejection =
  | 'too_short'
  | 'too_long'
  | 'control_character'
  | 'invisible_character'
  | 'disallowed_character'
  | 'markup';

/** Límites del campo en caracteres Unicode tras NFC, como `FreeTextField`. */
export interface FreeTextLimits {
  readonly minLength: number;
  readonly maxLength: number;
}

export type FreeTextVerdict =
  | { readonly ok: true; readonly normalized: string }
  | { readonly ok: false; readonly reason: FreeTextRejection; readonly key: string };

const EXPANSION_FACTOR = 4;

/**
 * `_MARKUP` del servidor. Python compara sin distinguir mayúsculas con su propio plegado, en el
 * que `[A-Za-z]` también admite `İ` (U+0130), `ı` (U+0131), `ſ` (U+017F) y el signo kelvin
 * (U+212A); con la bandera `u`, JavaScript pliega `ſ` y el kelvin, y los otros dos se nombran.
 */
const MARKUP =
  /<[a-z\u{130}\u{131}/!?]|&#[0-9]|&#x[0-9a-f]|&[a-z\u{130}\u{131}][a-z0-9\u{130}\u{131}]*;|&(?:lt|gt|amp|quot|apos|nbsp)/iu;

const CATEGORIES: readonly (readonly [RegExp, FreeTextRejection])[] = [
  [/^[\p{Cc}\p{Zl}\p{Zp}]$/u, 'control_character'],
  [/^\p{Cf}$/u, 'invisible_character'],
  [/^[\p{Cs}\p{Co}\p{Cn}]$/u, 'disallowed_character'],
];

/** `_DEFAULT_IGNORABLE` del servidor: `Default_Ignorable_Code_Point` más U+2800. */
const DEFAULT_IGNORABLE: readonly (readonly [number, number])[] = [
  [0x00ad, 0x00ad],
  [0x034f, 0x034f],
  [0x061c, 0x061c],
  [0x115f, 0x1160],
  [0x17b4, 0x17b5],
  [0x180b, 0x180f],
  [0x200b, 0x200f],
  [0x202a, 0x202e],
  [0x2060, 0x206f],
  [0x2800, 0x2800],
  [0x3164, 0x3164],
  [0xfe00, 0xfe0f],
  [0xfeff, 0xfeff],
  [0xffa0, 0xffa0],
  [0xfff0, 0xfff8],
  [0x1bca0, 0x1bca3],
  [0x1d173, 0x1d17a],
  [0xe0000, 0xe0fff],
];

/** Caracteres Unicode de `text` (un sustituto suelto cuenta como uno, como en Python). */
export function codePointLength(text: string): number {
  let count = 0;
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) index += 1;
    }
    count += 1;
  }
  return count;
}

function rejectedCharacter(char: string): FreeTextRejection | null {
  for (const [category, reason] of CATEGORIES) {
    if (category.test(char)) return reason;
  }
  const code = char.codePointAt(0) ?? 0;
  if (DEFAULT_IGNORABLE.some(([low, high]) => low <= code && code <= high)) {
    return 'invisible_character';
  }
  return null;
}

function reject(reason: FreeTextRejection): FreeTextVerdict {
  return { ok: false, reason, key: `validation.free_text.${reason}` };
}

/** Veredicto de la política base para `text` en un campo con `limits`. */
export function checkFreeText(text: string, limits: FreeTextLimits): FreeTextVerdict {
  const { minLength, maxLength } = limits;
  if (!Number.isInteger(minLength) || !Number.isInteger(maxLength) || minLength < 0) {
    throw new RangeError('límites de longitud incoherentes');
  }
  if (maxLength < Math.max(minLength, 1)) throw new RangeError('límites de longitud incoherentes');
  if (codePointLength(text) > EXPANSION_FACTOR * maxLength) return reject('too_long');
  const normalized = text.normalize('NFC');
  for (const char of normalized) {
    const reason = rejectedCharacter(char);
    if (reason !== null) return reject(reason);
  }
  if (MARKUP.test(normalized) || MARKUP.test(normalized.normalize('NFKC'))) return reject('markup');
  const length = codePointLength(normalized);
  if (length > maxLength) return reject('too_long');
  if (length < minLength) return reject('too_short');
  return { ok: true, normalized };
}
