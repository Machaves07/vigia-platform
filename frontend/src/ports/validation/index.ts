/**
 * `ports.validation` (LC-APP-16, PAT-APP-SEG-05, BR-APP-42 y 64, NFR-APP-20).
 *
 * - Al enviar: `validateBody` y `validateQuery` con los esquemas espejo que `npm run generate`
 *   deriva de `backend/openapi/app.yaml` (`openapi/bodies/`), compilados por el Ajv del contrato
 *   en código autónomo (`src/client/validators.js`): mismas longitudes, formatos, patrones y
 *   obligatoriedad que Pydantic, y ningún campo fuera del tipo (`additionalProperties: false`).
 *   Cada problema llega como `{field, key}`, con la clave del catálogo de textos, nunca con el
 *   mensaje de Ajv. Nunca es la barrera: el servidor valida todo y su mensaje prevalece.
 * - Texto libre: `checkFreeText` (`freeText.ts`), espejo de la política base de U-02.
 * - Al recibir: `validateContractFinding` y `validateContractHeartbeat` (`contract.ts`).
 */
import type { ErrorObject } from 'ajv';

import {
  bodies,
  queries,
  type BodyOperationId,
  type QueryOperationId,
} from '@/client/validators.js';
import { errorField, type CompiledValidator } from '@/contracts';

export { checkFreeText, type FreeTextLimits, type FreeTextVerdict } from './freeText';
export {
  validateContractFinding,
  validateContractHeartbeat,
  type ContractViolation,
} from './contract';
export type { BodyOperationId, QueryOperationId } from '@/client/validators.js';

/** Un problema estructural: el campo (ruta del contrato) y la clave de su texto. */
export interface ValidationIssue {
  readonly field: string | null;
  readonly key: string;
}

export type ValidationResult =
  { readonly ok: true } | { readonly ok: false; readonly issues: readonly ValidationIssue[] };

const KEYS: Readonly<Record<string, string>> = {
  required: 'validation.required',
  additionalProperties: 'validation.unknown_field',
  minLength: 'validation.too_short',
  maxLength: 'validation.too_long',
  minItems: 'validation.too_few_items',
  maxItems: 'validation.too_many_items',
  uniqueItems: 'validation.duplicate_items',
  minimum: 'validation.out_of_range',
  maximum: 'validation.out_of_range',
  exclusiveMinimum: 'validation.out_of_range',
  exclusiveMaximum: 'validation.out_of_range',
  pattern: 'validation.pattern',
  enum: 'validation.not_allowed',
  const: 'validation.not_allowed',
  type: 'validation.type',
};

const FORMAT_KEYS: Readonly<Record<string, string>> = {
  'date-time': 'validation.format_date_time',
  uuid: 'validation.format_uuid',
  email: 'validation.format_email',
};

function keyOf(error: ErrorObject): string {
  if (error.keyword === 'format') {
    const { format } = error.params as { format?: unknown };
    return (typeof format === 'string' ? FORMAT_KEYS[format] : undefined) ?? 'validation.invalid';
  }
  return KEYS[error.keyword] ?? 'validation.invalid';
}

/** Un problema por campo: el primero que Ajv informa (el más concreto). */
function issues(validator: CompiledValidator): ValidationIssue[] {
  const byField = new Map<string | null, ValidationIssue>();
  for (const error of validator.errors ?? []) {
    const field = errorField(error);
    if (!byField.has(field)) byField.set(field, { field, key: keyOf(error) });
  }
  if (byField.size === 0) return [{ field: null, key: 'validation.invalid' }];
  return [...byField.values()];
}

function run(validator: CompiledValidator, data: unknown): ValidationResult {
  return validator(data) ? { ok: true } : { ok: false, issues: issues(validator) };
}

/** Valida el cuerpo JSON que un puerto va a enviar a `operationId`. */
export function validateBody(operationId: BodyOperationId, body: unknown): ValidationResult {
  return run(bodies[operationId], body);
}

/** Valida los parámetros de consulta que un puerto va a enviar a `operationId`. */
export function validateQuery(operationId: QueryOperationId, query: unknown): ValidationResult {
  return run(queries[operationId], query);
}
