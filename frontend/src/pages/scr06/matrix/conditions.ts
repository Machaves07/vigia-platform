/**
 * Condiciones de señal de una fila de la matriz (postura × estado de señal; IF-13 paso 3,
 * gramática del predicado del contrato §3.2.1): cada condición es `{presence: true}` o
 * `{signal_role, value}`.
 *
 * Las etiquetas salen del catálogo del contrato (`condition_signal_role`, `condition_signal_value`),
 * nunca de los códigos, y la pantalla no inventa texto: el contrato admite la condición como
 * objeto libre, así que una forma desconocida se presenta como tal.
 */
import { text } from '@/texts';
import { label, labelInline } from '@/texts/labels';

/** Una condición de señal de la fila, en texto. */
function conditionText(condition: Readonly<Record<string, unknown>>): string {
  if (condition.presence === true) return text('scr06.matrix.condition.presence');
  const role = condition.signal_role;
  const value = condition.value;
  if (typeof role === 'string' && typeof value === 'string') {
    return `${label('condition_signal_role', role)} ${labelInline('condition_signal_value', value)}`;
  }
  return text('common.unknown');
}

/** Las condiciones de una fila en una línea; sin condiciones, su texto propio. */
export function conditionsText(conditions: readonly Readonly<Record<string, unknown>>[]): string {
  if (conditions.length === 0) return text('scr06.matrix.condition.none');
  return conditions.map(conditionText).join(' · ');
}
