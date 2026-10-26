/** Puerto de SCR-04: versiones inmutables del catálogo y declaración de estándares. */
import type { components } from '@/client/schema';
import {
  validateDeclaredStandard,
  validateZoneCatalog,
  validateSignedZoneCatalog,
  type ZoneCatalog,
} from '@/contracts';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
export { checkFreeText } from './validation';

export type StandardBody = components['schemas']['StandardBody'];
export type CatalogVersion = components['schemas']['CatalogVersionOut'];
export type CatalogDetail = components['schemas']['CatalogVersionDetail'];
export type CatalogSummary = components['schemas']['CatalogVersionSummary'];
export type Plant = components['schemas']['PlantView'];
export type Standard = ZoneCatalog['standards'][number];
export type Catalog = ZoneCatalog;
export type VersionBody = components['schemas']['StandardVersionBody'];
export type RetirementBody = components['schemas']['RetirementBody'];
export type OccupancyBody = components['schemas']['SingleOccupancyBody'];
/** catalog.domain.catalog_version: OpenAPI no publica estos límites de validación de dominio. */
export const CATALOG_REASON_LIMITS = { minLength: 10, maxLength: 500 } as const;

export const catalogPort = defineScreenPort('SCR-04', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  admissions: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_admissions_plants__plant_id__admissions_get',
    permission: 'catalog.read',
  },
  current: {
    kind: 'read',
    status: 'fixed',
    operationId: 'current_catalog_zones__zone_id__catalog_get',
    permission: 'catalog.read',
  },
  history: {
    kind: 'read',
    status: 'fixed',
    operationId: 'catalog_history_zones__zone_id__catalog_versions_get',
    permission: 'catalog.read',
  },
  version: {
    kind: 'read',
    status: 'fixed',
    operationId: 'catalog_version_zones__zone_id__catalog_versions__catalog_version__get',
    permission: 'catalog.read',
  },
  declare: {
    kind: 'write',
    status: 'fixed',
    operationId: 'new_standard_zones__zone_id__standards_post',
    permission: 'catalog.manage',
  },
  newVersion: {
    kind: 'write',
    status: 'fixed',
    operationId: 'new_standard_version_zones__zone_id__standards__standard_id__versions_post',
    permission: 'catalog.manage',
  },
  retire: {
    kind: 'write',
    status: 'fixed',
    operationId: 'retirement_zones__zone_id__standards__standard_id__retirement_post',
    permission: 'catalog.manage',
  },
  occupancy: {
    kind: 'write',
    status: 'fixed',
    operationId: 'single_occupancy_zones__zone_id__catalog_single_occupancy_put',
    permission: 'catalog.manage',
  },
  regression: {
    kind: 'read',
    status: 'fixed',
    operationId: 'regression_state_zones__zone_id__regression_get',
    permission: 'catalog.read',
  },
});

export function catalogClock() {
  return currentTransport().clock;
}

/** Ejemplos de entrada para componer sobre las cuatro familias; Ajv sigue siendo el oráculo. */
export function predicateTemplate(family: StandardBody['family']) {
  const signal =
    family === 'guard_bypass'
      ? 'guard'
      : family === 'startup_transition'
        ? 'start_command'
        : 'energy';
  return JSON.stringify(
    {
      all_of: [{ presence: true }, { signal_role: signal, value: 'asserted' }],
      min_duration_ms: family === 'dwell' ? 1000 : 0,
    },
    null,
    2,
  );
}

/**
 * El validador Ajv del contrato empareja familia y predicado sin metadatos inventados: con solo
 * `family` y `predicate`, Ajv evalúa primero el `oneOf` raíz (familia × predicado) y, si lo
 * cumple, el primer fallo es un campo obligatorio de los que asigna la plataforma. Las pruebas de
 * cada familia fijan ese orden: si cambiara, los predicados inválidos dejarían de rechazarse.
 */
export function parsePredicate(family: string, source: string): Record<string, unknown> | null {
  try {
    const predicate: unknown = JSON.parse(source);
    // Sin metadatos el documento nunca es válido: un `true` sería un esquema distinto.
    if (validateDeclaredStandard({ family, predicate })) return null;
    // `family` y `predicate` siempre están: el campo que falta en la raíz es un metadato.
    const first = validateDeclaredStandard.errors?.[0];
    const onlyMetadataMissing = first?.keyword === 'required' && first.instancePath === '';
    return onlyMetadataMissing ? (predicate as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** El diccionario genérico de OpenAPI no sustituye al esquema del contrato. */
export function catalogPayload(
  version: CatalogVersion,
  expectedZoneId?: string,
  expectedOrganizationId?: string,
  concessionOrganizationId: string | null = null,
): Catalog {
  if (!validateZoneCatalog(version.catalog)) throw new Error('Catálogo fuera del contrato.');
  if (
    expectedZoneId !== undefined &&
    (version.zone_id !== expectedZoneId || version.catalog.zone_id !== expectedZoneId)
  )
    throw new Error('Catálogo de otra zona.');
  if (
    expectedOrganizationId !== undefined &&
    !catalogOrganizationAllowed(
      version.catalog.organization_id,
      expectedOrganizationId,
      concessionOrganizationId,
    )
  )
    throw new Error('Catálogo de otra organización.');
  return version.catalog;
}

/** La concesión activa identifica al cliente mediante GET /hierarchy, nunca mediante el catálogo. */
export function catalogOrganizationAllowed(
  organizationId: string,
  expectedOrganizationId: string,
  concessionOrganizationId: string | null = null,
): boolean {
  return organizationId === expectedOrganizationId || organizationId === concessionOrganizationId;
}

export async function readVersion(
  zoneId: string,
  catalogVersion: number,
  organizationId: string,
  signal: AbortSignal,
  concessionOrganizationId: string | null = null,
) {
  const version = await catalogPort.version.read(
    { path: { zone_id: zoneId, catalog_version: catalogVersion } },
    signal,
  );
  catalogPayload(version, zoneId, organizationId, concessionOrganizationId);
  if (!validateSignedZoneCatalog(version.envelope)) throw new Error('Sobre fuera del contrato.');
  return version;
}

export async function admittedFamilies(plantId: string, signal: AbortSignal) {
  const families = new Set<StandardBody['family']>();
  const seen = new Set<string>();
  let after: string | null = null;
  do {
    const page = await catalogPort.admissions.read(
      { path: { plant_id: plantId }, query: { after, limit: 200 } },
      signal,
    );
    for (const admission of page.admissions)
      if (admission.result === 'admitted') families.add(admission.family);
    after = page.next_after;
    if (after !== null) {
      if (seen.has(after)) throw new Error('Cursor de admisiones repetido.');
      seen.add(after);
    }
  } while (after !== null);
  return [...families];
}

export function catalogFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http' && failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

const ERROR_FIELDS: Readonly<Record<string, string>> = {
  catalog_family_not_admitted: 'family',
  catalog_predicate_invalid: 'predicate',
  catalog_free_text_rejected: 'declared_text',
  catalog_zone_without_cameras: 'form',
  catalog_last_standard_in_zone: 'effective_from',
  reason_required: 'reason_es',
};
export function catalogNotice(error: Error | null): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const failure = error.failure;
  const detail = failure.error.detail_code ?? '';
  return {
    id: `scr04:${detail}`,
    error: failure.error,
    field: failure.behaviour.kind === 'anchored' ? (ERROR_FIELDS[detail] ?? 'form') : null,
  };
}
