/**
 * Validación al recibir de los datos del contrato (BR-APP-64, NFR-APP-28, PR-APP-19).
 *
 * Un `Finding` o un `Heartbeat` que llega de la plataforma se valida con el validador Ajv
 * compilado de `@vigia/contracts` (estricto: sin campos fuera del esquema, sin coerción) antes de
 * mostrarse. Si no cumple, el resultado es un error de contrato `contract_violation`, que la
 * persona ve como `internal_error`, y el objeto nunca se pinta: no se devuelve.
 */
import {
  contractIssue,
  validateFinding,
  validateHeartbeat,
  type ContractValidator,
  type Finding,
  type Heartbeat,
} from '@/contracts';

/** Error de contrato: no se muestra su detalle, solo `internal_error`. */
export interface ContractViolation {
  readonly code: 'contract_violation';
  readonly shownAs: 'internal_error';
  /** Esquema del contrato que no se cumplió (p. ej. `finding.schema.json`). */
  readonly schema: string;
  /** Ruta del primer campo que falla (`cameras[0].clips[0].sha256`), o `null`. */
  readonly field: string | null;
}

export type ContractCheck<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: ContractViolation };

function check<T>(validator: ContractValidator<T>, data: unknown): ContractCheck<T> {
  if (validator(data)) return { ok: true, value: data };
  return {
    ok: false,
    error: {
      code: 'contract_violation',
      shownAs: 'internal_error',
      schema: validator.schema,
      field: contractIssue(validator)?.field ?? null,
    },
  };
}

/** `Finding` recibido, validado contra `finding.schema.json`. */
export function validateContractFinding(data: unknown): ContractCheck<Finding> {
  return check(validateFinding, data);
}

/** `Heartbeat` recibido, validado contra `heartbeat.schema.json`. */
export function validateContractHeartbeat(data: unknown): ContractCheck<Heartbeat> {
  return check(validateHeartbeat, data);
}
