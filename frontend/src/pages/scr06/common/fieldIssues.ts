/**
 * Un error estructural que el puerto devolvió sin enviar la petición (BR-APP-42, 64): el espejo
 * del esquema (`validateBody`) informa `{field, key}` con la clave del catálogo, y la pantalla lo
 * pinta junto al campo en vez de copiar la regla. Aquí solo se indexan esos problemas por campo.
 */
import { isPortError } from '@/ports/shared';

export type FieldIssues = ReadonlyMap<string, string>;

export const NO_FIELD_ISSUES: FieldIssues = new Map();

export function fieldIssues(error: unknown): FieldIssues {
  if (!isPortError(error)) return NO_FIELD_ISSUES;
  const { failure } = error;
  if (failure.kind !== 'invalid_input') return NO_FIELD_ISSUES;
  const issues = new Map<string, string>();
  for (const issue of failure.issues) {
    if (typeof issue.field === 'string' && issue.field !== '') issues.set(issue.field, issue.key);
  }
  return issues;
}
