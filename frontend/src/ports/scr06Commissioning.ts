/**
 * Puerto de la consola de comisionamiento y walk-test (SCR-06, LC-APP-14; IF-13 pasos 1 a 4;
 * U-03 §3.3 y «Versión 1.5»).
 *
 * Ocho operaciones `fixed` del contrato de U-03: la sesión vigente de la zona y el selector de
 * clips de comisionamiento (lecturas, `catalog.read`), y las escrituras del instalador
 * (`commissioning.run`): abrir la sesión, abrir y cerrar pasos, registrar pases, reabrir una
 * sesión incompleta y enviar la muestra de exposición del tramo 3a (BR-APP-84).
 *
 * La muestra de exposición se envía en segundo plano y **sin reintento**: `ports.base` no
 * reintenta escrituras (BR-APP-72) y la pantalla no la repite; una muestra fallida no bloquea el
 * pase y el acta la marca como «no medido». El permiso y el alcance los decide el servidor
 * (BR-APP-43): la pantalla solo declara la clave.
 */
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

/** Mínimo de pases por celda de la matriz (U-03 §3.3, respuesta 9 = A). */
export const MIN_PASSES_PER_CELL = 3;

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
});
