/**
 * Puerto de SCR-09 para la clasificación y el lazo (SUP-U05-02, 03, 04 y 07). Las listas cerradas
 * salen de las etiquetas de U-04 (`labels.loop.es.json`), nunca de una copia: la pantalla ofrece
 * los valores que la unidad dueña etiqueta y el servidor decide si los admite.
 */
import loop from '../../../backend/resources/labels.loop.es.json';
import type { components } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import type { FreeTextLimits } from '@/ports/validation';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type FindingDetail = components['schemas']['FindingDetailOut'];
export type ClassificationHistory = components['schemas']['ClassificationHistoryOut'];
export type ClassificationItem = components['schemas']['ClassificationItemOut'];
export type ClassificationBody = components['schemas']['ClassificationBody'];
export type Classification = components['schemas']['ClassificationOut'];
export type ActionItem = components['schemas']['ActionOut'];
export type ActionBody = components['schemas']['CorrectiveActionBody'];
export type Action = components['schemas']['CorrectiveActionOut'];
export type LoopConfiguration = components['schemas']['ConfigurationOut'];
export type Plant = components['schemas']['PlantView'];

export const classifyPort = defineScreenPort('SCR-09', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  detail: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_finding_loop_findings__finding_state_id__get',
    permission: 'findings.read',
  },
  history: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_classifications_loop_findings__finding_state_id__classifications_get',
    permission: 'findings.read',
  },
  classify: {
    kind: 'write',
    status: 'fixed',
    operationId: 'classify_loop_findings__finding_state_id__classifications_post',
    permission: 'findings.classify',
  },
  configuration: {
    kind: 'read',
    status: 'fixed',
    operationId: 'configuration_loop_plants__plant_id__configuration_get',
    permission: 'hierarchy.read',
  },
  assign: {
    kind: 'write',
    status: 'fixed',
    operationId: 'assign_loop_findings__finding_state_id__actions_post',
    permission: 'actions.manage',
  },
});

/** Salidas de `classification_outcome` (H-21), en el orden de la etiqueta de U-04. */
export const OUTCOMES: readonly string[] = Object.keys(loop.classification_outcome);

/** Lista de categorías de motivo de cada salida (DE §1 de U-04). */
const REASON_LISTS = {
  confirmed: 'reason_category_confirmed',
  authorized_operation: 'reason_category_authorized',
  false_positive: 'reason_category_false_positive',
} as const;

/** Categorías de motivo de la salida, con el nombre de su lista para `label()`. */
export function reasonCategories(outcome: string): { list: string; values: readonly string[] } {
  if (!Object.hasOwn(REASON_LISTS, outcome)) return { list: '', values: [] };
  const list = REASON_LISTS[outcome as keyof typeof REASON_LISTS];
  return { list, values: Object.keys(loop[list]) };
}

/** Los nueve estados de `finding_lifecycle_state` (P2: ninguno es «sin hallazgos»). */
export const LIFECYCLE_STATES: readonly string[] = Object.keys(loop.finding_lifecycle_state);

/** Tipos de `action_type` (H-24): solo sobre la zona, nunca sobre una persona. */
export const ACTION_TYPES: readonly string[] = Object.keys(loop.action_type);

/** Estados del lazo con una acción comprometida o por comprometer (lazo abierto). */
const OPEN_LOOP: ReadonlySet<string> = new Set(['open', 'action_assigned', 'overdue']);

export function loopIsOpen(state: string): boolean {
  return OPEN_LOOP.has(state);
}

/**
 * Límites de `reason_text` de la clasificación (`ReasonText` de U-04, 20 a 1000). El esquema de
 * `ClassificationBody` no los publica; la prueba de la pantalla los compara con la ruta
 * `classification/reason_text` del corpus exportado `u05-fixtures.json`. El servidor decide.
 */
export const CLASSIFICATION_REASON_LIMITS: FreeTextLimits = { minLength: 20, maxLength: 1000 };

export function classifyClock() {
  return currentTransport().clock;
}

export function loopFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http' && failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

/**
 * Aviso del servidor para el formulario: los rechazos anclados van al campo que nombra su
 * `detail_code` (`fields`) o, si no nombran ninguno, al formulario (`form`). `null` si el fallo no
 * es una respuesta del servidor.
 */
export function writeNotice(
  error: Error | null,
  fields: Readonly<Record<string, string>>,
): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const detail = error.failure.error.detail_code ?? '';
  const field =
    error.failure.behaviour.kind === 'anchored'
      ? ((Object.hasOwn(fields, detail) ? fields[detail] : undefined) ?? 'form')
      : null;
  return { id: `scr09:${detail}`, error: error.failure.error, field };
}

/** El fallo local del puerto (cuerpo que no cumple su esquema, sin conexión), si lo hay. */
export function localFailure(error: Error | null): 'invalid_input' | 'unavailable' | null {
  if (error === null || !isPortError(error)) return error === null ? null : 'unavailable';
  if (error.failure.kind === 'invalid_input') return 'invalid_input';
  if (error.failure.kind === 'http') return null;
  return 'unavailable';
}
