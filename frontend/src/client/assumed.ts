/**
 * Operaciones `assumed` (BR-APP-63, PAT-APP-MAN-07): las que los puertos del diseño usan y
 * `backend/openapi/app.yaml` aún no tiene. Este archivo es a mano; `OPERATIONS` es generado.
 *
 * `DESIGN_OPERATIONS` copia las operaciones con ruta de `frontend-components.md` §3 (SCR-01 a
 * SCR-16), con las abreviaturas del diseño desplegadas (`GET/POST /users` son dos;
 * `POST /users/{id}/deactivate`, `/reactivate` también). Las de U-04 que el diseño describe sin
 * ruta (tabla `SUP-U05-nn`) no figuran: sus puertos (VIG-292 en adelante) las nombran por su ruta
 * real de `app.yaml`. La diferencia entre versiones del catálogo no figura (adenda A-54).
 *
 * `isAssumed` responde por cualquier operación, figure o no en el diseño: lo que no está en la
 * especificación es `assumed`, y su puerto responde `unavailable` (VIG-292).
 */
import { OPERATIONS } from './index';

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Operación por método y ruta, con los parámetros con cualquier nombre: `POST /zones/{id}/gates`. */
export type OperationKey = `${Method} /${string}`;

export interface DesignOperation {
  readonly screen: `SCR-${string}`;
  readonly operation: OperationKey;
}

const op = (screen: `SCR-${string}`, operation: OperationKey): DesignOperation => ({
  screen,
  operation,
});

export const DESIGN_OPERATIONS: readonly DesignOperation[] = [
  op('SCR-01', 'POST /auth/login'),
  op('SCR-01', 'POST /auth/second-factor'),
  op('SCR-01', 'POST /auth/second-factor/enroll'),
  op('SCR-01', 'POST /auth/logout'),
  op('SCR-01', 'POST /auth/password'),
  op('SCR-01', 'GET /auth/sessions'),
  op('SCR-01', 'POST /auth/sessions/close-others'),
  op('SCR-01', 'POST /invitations/{token}/accept'),
  op('SCR-01', 'GET /me'),
  op('SCR-01', 'GET /users'),
  op('SCR-01', 'POST /users'),
  op('SCR-01', 'PATCH /users/{id}'),
  op('SCR-01', 'POST /users/{id}/deactivate'),
  op('SCR-01', 'POST /users/{id}/reactivate'),
  op('SCR-01', 'POST /users/{id}/second-factor/reset'),
  op('SCR-01', 'POST /users/{id}/roles'),
  op('SCR-01', 'DELETE /users/{id}/roles/{assignment}'),
  op('SCR-01', 'GET /hierarchy'),
  op('SCR-01', 'POST /plants'),
  op('SCR-01', 'POST /plants/{id}/zones'),
  op('SCR-01', 'GET /organization/settings'),
  op('SCR-01', 'PATCH /organization/settings'),
  op('SCR-02', 'GET /concessions'),
  op('SCR-02', 'GET /concessions/{id}/queries'),
  op('SCR-02', 'POST /concessions/{id}/revoke'),
  op('SCR-02', 'POST /provider/concessions'),
  op('SCR-02', 'GET /provider/concessions'),
  op('SCR-03', 'POST /integrity/verify'),
  op('SCR-03', 'GET /integrity/results'),
  op('SCR-03', 'GET /integrity/checkpoints'),
  op('SCR-03', 'GET /.well-known/vigia-checkpoint-keys'),
  op('SCR-03', 'GET /.well-known/vigia-verifier'),
  op('SCR-03', 'GET /zones/{id}/coverage'),
  op('SCR-03', 'GET /zones/{id}/coverage/at'),
  op('SCR-04', 'GET /zones/{id}/catalog'),
  op('SCR-04', 'GET /zones/{id}/catalog/versions'),
  op('SCR-04', 'GET /zones/{id}/catalog/versions/{v}'),
  op('SCR-04', 'POST /zones/{id}/standards'),
  op('SCR-04', 'POST /zones/{id}/standards/{sid}/versions'),
  op('SCR-04', 'POST /zones/{id}/standards/{sid}/retirement'),
  op('SCR-04', 'PUT /zones/{id}/catalog/single-occupancy'),
  op('SCR-04', 'POST /plants/{id}/admissions'),
  op('SCR-04', 'GET /plants/{id}/admissions'),
  op('SCR-04', 'GET /zones/{id}/regression'),
  op('SCR-05', 'GET /zones/{id}/gates'),
  op('SCR-05', 'POST /zones/{id}/gates/mounting/scope-record'),
  op('SCR-05', 'PUT /plants/{id}/signatory-policy'),
  op('SCR-05', 'GET /plants/{id}/signatory-policy'),
  op('SCR-05', 'POST /zones/{id}/use-agreements'),
  op('SCR-05', 'POST /use-agreements/{id}/confirmations'),
  op('SCR-05', 'POST /use-agreements/{id}/approval'),
  op('SCR-05', 'POST /zones/{id}/gates/{gate}/revocation'),
  op('SCR-05', 'POST /plants/{id}/policy'),
  op('SCR-05', 'GET /plants/{id}/policy'),
  op('SCR-05', 'POST /documents'),
  op('SCR-06', 'POST /zones/{id}/live-view-token'),
  op('SCR-06', 'POST /zones/{id}/walk-tests'),
  op('SCR-06', 'GET /zones/{id}/walk-tests/current'),
  op('SCR-06', 'POST /walk-tests/{id}/steps'),
  op('SCR-06', 'POST /walk-tests/{id}/steps/{sid}/close'),
  op('SCR-06', 'POST /walk-tests/{id}/passes'),
  op('SCR-06', 'POST /walk-tests/{id}/occlusion-tests'),
  op('SCR-06', 'POST /walk-tests/{id}/close'),
  op('SCR-06', 'POST /walk-tests/{id}/reopen'),
  op('SCR-06', 'POST /walk-tests/{id}/exposure-samples'),
  op('SCR-06', 'GET /commissioning-records/{id}'),
  op('SCR-06', 'GET /commissioning-records/{id}/document'),
  op('SCR-06', 'POST /zones/{id}/walk-tests/regression-rerun'),
  op('SCR-07', 'GET /fleet/nodes'),
  op('SCR-07', 'GET /fleet/nodes/{id}'),
  op('SCR-07', 'POST /plants/{id}/nodes'),
  op('SCR-07', 'POST /nodes/{id}/zones'),
  op('SCR-07', 'POST /nodes/{id}/zones/{zid}/unassignment'),
  op('SCR-07', 'POST /nodes/{id}/enrollment-codes'),
  op('SCR-07', 'GET /nodes/{id}/enrollment-attempts'),
  op('SCR-07', 'POST /nodes/{id}/revocation'),
  op('SCR-07', 'POST /nodes/{id}/decommission'),
  op('SCR-07', 'POST /fleet/target-versions'),
  op('SCR-07', 'GET /fleet/alarms'),
  op('SCR-07', 'GET /plants/{id}/fleet-thresholds'),
  op('SCR-07', 'PUT /plants/{id}/fleet-thresholds'),
  op('SCR-08', 'GET /ledger/records/{id}'),
  op('SCR-08', 'POST /evidence/{id}/read-url'),
  op('SCR-08', 'GET /labels'),
  op('SCR-08', 'GET /zones/{id}/coverage/at'),
  op('SCR-09', 'POST /evidence/{id}/read-url'),
  op('SCR-13', 'GET /zones/{id}/coverage'),
  op('SCR-14', 'GET /integrity/checkpoints'),
  op('SCR-14', 'GET /.well-known/vigia-verifier'),
  op('SCR-16', 'POST /zones/{id}/live-view-token'),
  op('SCR-16', 'GET /zones/{id}/transparency'),
  op('SCR-16', 'POST /use-agreements/{id}/confirmations'),
];

/** Método en mayúsculas y ruta con cada parámetro como `{}`: `POST /zones/{}/gates`. */
export function normalizeOperation(operation: string): string {
  const [method = '', path = ''] = operation.split(' ');
  return `${method.toUpperCase()} ${path.replace(/\{[^}]*\}/g, '{}')}`;
}

const SPECIFIED = new Set(
  Object.values(OPERATIONS).map(({ method, path }) => normalizeOperation(`${method} ${path}`)),
);

/** `true` si `operation` no está en `app.yaml`: su puerto responde `unavailable`. */
export function isAssumed(operation: OperationKey): boolean {
  return !SPECIFIED.has(normalizeOperation(operation));
}

/** Operaciones del diseño que `app.yaml` no tiene (en `main` se esperan cero). */
export const ASSUMED_OPERATIONS: readonly DesignOperation[] = DESIGN_OPERATIONS.filter(
  ({ operation }) => isAssumed(operation),
);
