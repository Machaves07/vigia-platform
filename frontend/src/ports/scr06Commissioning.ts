/**
 * Puerto de la consola de comisionamiento y walk-test (SCR-06, LC-APP-14; IF-13 pasos 1 a 7;
 * U-03 §3.3 y «Versión 1.5»).
 *
 * Doce operaciones `fixed` del contrato de U-03: la sesión vigente de la zona, el selector de
 * clips de comisionamiento y el acta cerrada (lecturas, `catalog.read`), y las escrituras del
 * instalador (`commissioning.run`): abrir la sesión, abrir y cerrar pasos, registrar pases,
 * reabrir una sesión incompleta, enviar la muestra de exposición del tramo 3a (BR-APP-84),
 * registrar la prueba de oclusión de una cámara, cerrar el acta y abrir la reejecución por
 * regresión. El documento legible del acta no pasa por el puerto: se abre **por navegación**
 * (`recordDocumentHref`), nunca por la caché (`UNCACHED_READS`).
 *
 * La muestra de exposición se envía en segundo plano y **sin reintento**: `ports.base` no
 * reintenta escrituras (BR-APP-72) y la pantalla no la repite; una muestra fallida no bloquea el
 * pase y el acta la marca como «no medido». El permiso y el alcance los decide el servidor
 * (BR-APP-43): la pantalla solo declara la clave.
 */
import { OPERATIONS } from '@/client';
import type { components } from '@/client/schema';

import { defineScreenPort } from './base';

/** Sesión de walk-test con lo registrado en ella (`WalkTestSessionOut` de `app.yaml`). */
export type WalkTestSession = components['schemas']['WalkTestSessionOut'];
/** Sesión vigente de la zona, o `null` si no hay ninguna (`CurrentWalkTestOut`). */
export type CurrentWalkTest = components['schemas']['CurrentWalkTestOut'];
/** Un paso de la sesión (`StepOut`): marcas del servidor y, aparte, su corrección. */
export type WalkTestStep = components['schemas']['StepOut'];
/** Un pase registrado (`PassOut`). */
export type WalkTestPass = components['schemas']['PassOut'];
/** Una fila de la matriz (postura × estado de señal) con el conteo de sus pases. */
export type MatrixRow = components['schemas']['MatrixRowOut'];
/** Un clip de verificación de la zona, candidato a evidencia de un pase. */
export type CommissioningClip = components['schemas']['CommissioningClipOut'];
/** Página de clips de comisionamiento (cursor por clave). */
export type CommissioningClips = components['schemas']['CommissioningClipsOut'];
/** Un pase registrado (`PassOut`). */
export type PassOut = components['schemas']['PassOut'];
/** Muestra de exposición del tramo 3a (`ExposureSampleOut`). */
export type ExposureSample = components['schemas']['ExposureSampleOut'];
/** Cuerpo de la muestra de exposición que envía la consola (BR-APP-84). */
export type ExposureSampleBody = components['schemas']['ExposureSampleBody'];
/** Posturas admitidas de la matriz (lista cerrada de U-03). */
export type Posture = components['schemas']['Posture'];
/** Resultado de un pase (lista cerrada de U-03). */
export type PassResult = components['schemas']['PassResult'];
/** Tipos de paso cronometrado (lista cerrada de U-03). */
export type StepKind = components['schemas']['StepKind'];

/** Prueba de oclusión de una cámara (`OcclusionTestOut`): nace `pending` con su `deadline`. */
export type OcclusionTest = components['schemas']['OcclusionTestOut'];
/** Cuerpo de la prueba de oclusión (`OcclusionTestBody`). */
export type OcclusionTestBody = components['schemas']['OcclusionTestBody'];
/** Resultado de la verificación de la oclusión (lista cerrada de U-03). */
export type OcclusionVerification = components['schemas']['OcclusionVerification'];
/** Cuerpo del cierre del acta (`CloseBody`). */
export type CloseBody = components['schemas']['CloseBody'];
/** Línea base de una cámara que teclea el instalador (`BaselineBody`). */
export type BaselineBody = components['schemas']['BaselineBody'];
/** Acta de comisionamiento estructurada (`CommissioningRecordOut`), sin responsable (H-53). */
export type CommissioningRecord = components['schemas']['CommissioningRecordOut'];
/** Un tramo de latencia del acta (`TrancheOut`), con el reloj que lo midió. */
export type LatencyTranche = components['schemas']['TrancheOut'];

/** Mínimo de pases por celda de la matriz (U-03 §3.3, respuesta 9 = A). */
export const MIN_PASSES_PER_CELL = 3;

/**
 * Rango de `installer_measurements.beacon_latency_ms_p95` (D-2, BR-APP-42): el p95 de la latencia
 * de la baliza que el instalador lee de `LocalStatus` del nodo, en milisegundos enteros. El
 * esquema del contrato no fija los límites; la pantalla los comprueba antes de enviar.
 */
export const BEACON_LATENCY_MIN_MS = 0;
export const BEACON_LATENCY_MAX_MS = 10_000;

const OCCLUSION_VERIFICATIONS: ReadonlySet<string> = new Set<OcclusionVerification>([
  'verified',
  'declared',
  'failed',
  'pending',
]);

function isText(value: unknown): value is string {
  return typeof value === 'string';
}

function isNullableText(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

/**
 * Las pruebas de oclusión de la sesión (`occlusion_tests`, que el contrato publica como objetos
 * sin esquema) con la forma de `OcclusionTestOut`. Lectura tolerante (BR-CTR-24): un campo nuevo
 * se ignora y una entrada sin los campos que la pantalla pinta se descarta en vez de mostrarse a
 * medias.
 */
export function occlusionTestsOf(session: WalkTestSession): OcclusionTest[] {
  const tests: OcclusionTest[] = [];
  for (const entry of session.occlusion_tests) {
    const verification = entry.verification;
    const events = entry.correlated_event_ids;
    const declared = entry.declared_reason_es ?? null;
    const failure = entry.failure_reason ?? null;
    if (
      !isText(entry.test_id) ||
      !isText(entry.camera_id) ||
      !isText(entry.started_at) ||
      !isText(entry.ended_at) ||
      !isText(entry.deadline) ||
      !isText(verification) ||
      !OCCLUSION_VERIFICATIONS.has(verification) ||
      !Array.isArray(events) ||
      !events.every(isText) ||
      !isNullableText(declared) ||
      !isNullableText(failure) ||
      !isText(entry.recorded_by)
    ) {
      continue;
    }
    tests.push({
      test_id: entry.test_id,
      camera_id: entry.camera_id,
      started_at: entry.started_at,
      ended_at: entry.ended_at,
      deadline: entry.deadline,
      verification: verification as OcclusionVerification,
      correlated_event_ids: events,
      declared_reason_es: declared,
      failure_reason: failure,
      recorded_by: entry.recorded_by,
    });
  }
  return tests;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Dirección del documento legible del acta (`GET /commissioning-records/{record_id}/document`):
 * del mismo origen, con la sesión de la cookie, y abierta por navegación (generado a demanda,
 * nunca guardado ni en caché). Un identificador que no es UUID no produce dirección.
 */
export function recordDocumentHref(recordId: string): string | null {
  if (!UUID.test(recordId)) return null;
  const template =
    OPERATIONS.commissioning_record_document_commissioning_records__record_id__document_get.path;
  return template.replace('{record_id}', encodeURIComponent(recordId));
}

export const scr06CommissioningPort = defineScreenPort('SCR-06', {
  /** `GET /zones/{zone_id}/walk-tests/current`: retoma la sesión de la zona, o `null`. */
  currentWalkTest: {
    kind: 'read',
    status: 'fixed',
    operationId: 'current_walk_test_zones__zone_id__walk_tests_current_get',
    permission: 'catalog.read',
  },
  /** `GET /zones/{zone_id}/commissioning-clips`: clips de comisionamiento (selector de evidencia). */
  commissioningClips: {
    kind: 'read',
    status: 'fixed',
    operationId: 'commissioning_clips_zones__zone_id__commissioning_clips_get',
    permission: 'catalog.read',
  },
  /** `POST /zones/{zone_id}/walk-tests`: abre la sesión con la matriz derivada del catálogo. */
  openWalkTest: {
    kind: 'write',
    status: 'fixed',
    operationId: 'open_walk_test_zones__zone_id__walk_tests_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/steps`: abre un paso cronometrado (`started_at` del servidor). */
  startStep: {
    kind: 'write',
    status: 'fixed',
    operationId: 'start_step_walk_tests__session_id__steps_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/steps/{step_id}/close`: cierra el paso, con corrección opcional. */
  closeStep: {
    kind: 'write',
    status: 'fixed',
    operationId: 'close_step_walk_tests__session_id__steps__step_id__close_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/passes`: registra un pase de una fila de la matriz. */
  recordPass: {
    kind: 'write',
    status: 'fixed',
    operationId: 'record_pass_walk_tests__session_id__passes_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/reopen`: reabre una sesión incompleta con motivo. */
  reopenWalkTest: {
    kind: 'write',
    status: 'fixed',
    operationId: 'reopen_walk_test_walk_tests__session_id__reopen_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/exposure-samples`: muestra del tramo de exposición 3a. */
  recordExposureSample: {
    kind: 'write',
    status: 'fixed',
    operationId: 'record_exposure_sample_walk_tests__session_id__exposure_samples_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/occlusion-tests`: prueba de oclusión (nace `pending`). */
  recordOcclusionTest: {
    kind: 'write',
    status: 'fixed',
    operationId: 'record_occlusion_test_walk_tests__session_id__occlusion_tests_post',
    permission: 'commissioning.run',
  },
  /** `POST /walk-tests/{session_id}/close`: cierra el acta con firmas y mediciones del instalador. */
  closeWalkTest: {
    kind: 'write',
    status: 'fixed',
    operationId: 'close_walk_test_walk_tests__session_id__close_post',
    permission: 'commissioning.run',
  },
  /** `GET /commissioning-records/{record_id}`: el acta estructurada (lectura auditada). */
  commissioningRecord: {
    kind: 'read',
    status: 'fixed',
    operationId: 'commissioning_record_commissioning_records__record_id__get',
    permission: 'catalog.read',
  },
  /** `POST /zones/{zone_id}/walk-tests/regression-rerun`: reejecución con las filas afectadas. */
  openRegressionRerun: {
    kind: 'write',
    status: 'fixed',
    operationId: 'open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post',
    permission: 'commissioning.run',
  },
});
