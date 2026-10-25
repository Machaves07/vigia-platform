/** Puerto de parámetros, admisión y vocabulario; el servidor autoriza y registra cada cambio. */
import type { components } from '@/client/schema';
import { label } from '@/texts/labels';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
import { checkFreeText, validateBody, type BodyOperationId } from './validation';

export type Admission = components['schemas']['AdmissionView'];
export type AdmissionBody = components['schemas']['AdmissionBody'];
export type Criterion = components['schemas']['AdmissionCriterion'];
export type Catalog = components['schemas']['CatalogVersionOut'];
export type Vocabulary = components['schemas']['VocabularyOut'];
export type VocabularyChange = components['schemas']['VocabularyChangeBody'];
export type Camera = components['schemas']['CameraBody'];
export type Signal = components['schemas']['SignalDeclaration'];
export type ParameterKind = 'cameras' | 'minimum_coverage' | 'signals' | 'thresholds' | 'windows';

export const parametersPort = defineScreenPort('SCR-04', {
  catalog: {
    kind: 'read',
    status: 'fixed',
    permission: 'catalog.read',
    operationId: 'current_catalog_zones__zone_id__catalog_get',
  },
  regression: {
    kind: 'read',
    status: 'fixed',
    permission: 'catalog.read',
    operationId: 'regression_state_zones__zone_id__regression_get',
  },
  cameras: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'cameras_zones__zone_id__cameras_put',
  },
  minimum_coverage: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'minimum_coverage_zones__zone_id__minimum_coverage_put',
  },
  signals: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'signals_zones__zone_id__signals_put',
  },
  thresholds: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'thresholds_zones__zone_id__thresholds_put',
  },
  windows: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'windows_zones__zone_id__windows_put',
  },
  admissions: {
    kind: 'read',
    status: 'fixed',
    permission: 'catalog.read',
    operationId: 'list_admissions_plants__plant_id__admissions_get',
  },
  evaluate: {
    kind: 'write',
    status: 'fixed',
    permission: 'catalog.manage',
    operationId: 'evaluate_plants__plant_id__admissions_post',
  },
  vocabulary: {
    kind: 'read',
    status: 'fixed',
    permission: 'vocabulary.manage',
    operationId: 'vocabulary_loop_vocabulary_get',
  },
  changeVocabulary: {
    kind: 'write',
    status: 'fixed',
    permission: 'vocabulary.manage',
    operationId: 'change_vocabulary_loop_vocabulary_changes_post',
  },
});

export const CRITERIA = ['standard', 'remedy', 'subject'] as const;
export const FAMILIES = ['coexistence', 'guard_bypass', 'dwell', 'startup_transition'] as const;
export const PARAMETER_OPERATIONS = {
  cameras: 'cameras_zones__zone_id__cameras_put',
  minimum_coverage: 'minimum_coverage_zones__zone_id__minimum_coverage_put',
  signals: 'signals_zones__zone_id__signals_put',
  thresholds: 'thresholds_zones__zone_id__thresholds_put',
  windows: 'windows_zones__zone_id__windows_put',
} as const;

/** Los cuerpos no publican longitudes; pruebas contrastan estos límites con el corpus dueño. */
export const parameterReasonLimits = { minLength: 10, maxLength: 500 } as const;
export const vocabularyReasonLimits = { minLength: 10, maxLength: 500 } as const;
export const parametersClock = () => currentTransport().clock;

/** UI-14 exige claves after_*; el token opaco de U-03 viaja sin alterarlo como after. */
export async function readAdmissions(
  plantId: string,
  cursor: Readonly<Record<string, string>> | null,
  limit: number,
  signal: AbortSignal,
) {
  const page = await parametersPort.admissions.read(
    { path: { plant_id: plantId }, query: { limit, after: cursor?.after_admission ?? null } },
    signal,
  );
  return {
    items: page.admissions,
    nextCursor: page.next_after === null ? null : { after_admission: page.next_after },
  };
}

export function screenLabel(group: string, value: string): string {
  return label(group, value);
}

export function reasonValid(reason: string, vocabulary = false): boolean {
  return (
    reason.trim() !== '' &&
    checkFreeText(reason, vocabulary ? vocabularyReasonLimits : parameterReasonLimits).ok
  );
}

export function bodyIssues(operation: BodyOperationId, body: unknown) {
  const result = validateBody(operation, body);
  return result.ok ? [] : result.issues;
}

export function parametersNotice(error: Error | null, field = 'form'): Notice | null {
  if (error === null) return null;
  if (isPortError(error) && error.failure.kind === 'http')
    return { id: 'scr04-parameters', error: error.failure.error, field };
  return { id: 'scr04-parameters', error: { code: 'temporarily_unavailable', status: 0 }, field };
}

export function parametersFailure(
  error: Error,
): 'error' | 'not_available_in_scope' | 'unavailable' {
  if (isPortError(error)) {
    if (error.failure.kind === 'http' && error.failure.behaviour.kind === 'not_available_in_scope')
      return 'not_available_in_scope';
    if (error.failure.kind === 'offline' || error.failure.kind === 'unavailable')
      return 'unavailable';
  }
  return 'error';
}

/** A-56: el código completo, nunca un 400 genérico, confirma la evaluación registrada. */
export function isRecordedRejection(error: unknown): boolean {
  if (!isPortError(error) || error.failure.kind !== 'http') return false;
  const response = error.failure.error;
  return (
    response.status === 400 &&
    response.code === 'invalid_request' &&
    response.detail_code === 'catalog_admission_rejected'
  );
}

/** El criterio se lee del 400 aditivo o, para la API actual, del historial autorizado. */
export async function admissionRejection(
  error: unknown,
  plantId: string,
  input: AdmissionBody,
): Promise<Criterion | null> {
  if (!isRecordedRejection(error) || !isPortError(error) || error.failure.kind !== 'http')
    return null;
  const response = error.failure.error;
  const body = response.body;
  if (typeof body === 'object' && body !== null && Object.hasOwn(body, 'failed_criterion')) {
    const criterion = (body as Record<string, unknown>).failed_criterion;
    return CRITERIA.find((candidate) => candidate === criterion) ?? null;
  }
  // U-03 actual conserva el ApiError cerrado: no se inventa el criterio desde las respuestas.
  try {
    const page = await parametersPort.admissions.read({
      path: { plant_id: plantId },
      query: { limit: 200 },
    });
    const recorded = page.admissions.find(
      (entry) =>
        entry.result === 'rejected' &&
        entry.family === input.family &&
        CRITERIA.every((criterion) => entry.answers[criterion] === input.answers[criterion]),
    );
    return recorded?.failed_criterion ?? null;
  } catch {
    return null;
  }
}
