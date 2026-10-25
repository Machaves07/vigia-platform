/** SCR-12: revisión mensual (SUP-U05-24 a 29 y 41); sin operación de clasificación. */
import type { components } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
import type { HttpError } from './errors/map';

export type MonthlyPackage = components['schemas']['MonthlyPackageOut'];
export type Commitment = components['schemas']['CommitmentOut'];
export type CommitmentInput = components['schemas']['CommitmentIn'];
export type CommitmentUpdate = components['schemas']['CommitmentUpdateBody'];
export type SampleItem = components['schemas']['SampleItemOut'];
export type SampleResult = components['schemas']['SampleResultBody'];

export const monthlyPort = defineScreenPort('SCR-12', {
  package: {
    kind: 'read',
    status: 'fixed',
    permission: 'review_package.read',
    operationId: 'get_monthly_package_accreditation_monthly_packages_get',
  },
  sample: {
    kind: 'read',
    status: 'fixed',
    permission: 'review_package.read',
    operationId:
      'get_monthly_package_sample_accreditation_monthly_packages__package_id__sample_get',
  },
  result: {
    kind: 'write',
    status: 'fixed',
    permission: 'findings.classify',
    operationId: 'record_sample_result_accreditation_sample_items__sample_item_id__result_post',
  },
  recordCommitments: {
    kind: 'write',
    status: 'fixed',
    permission: 'commitments.write',
    operationId: 'record_commitments_accreditation_monthly_packages__package_id__commitments_post',
  },
  updateCommitment: {
    kind: 'write',
    status: 'fixed',
    permission: 'commitments.write',
    operationId: 'update_commitment_accreditation_commitments__commitment_id__patch',
  },
  commitments: {
    kind: 'read',
    status: 'fixed',
    permission: 'review_package.read',
    operationId: 'list_commitments_accreditation_commitments_get',
  },
  download: {
    kind: 'read',
    status: 'fixed',
    permission: 'review_package.read',
    operationId:
      'get_monthly_package_plant_download_accreditation_monthly_packages__plant_id___period__download_get',
  },
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    permission: 'session',
    operationId: 'hierarchy_hierarchy_get',
  },
});

export function monthlyClock() {
  return currentTransport().clock;
}

export function monthlyHttpError(error: unknown): HttpError {
  if (isPortError(error) && error.failure.kind === 'http') return error.failure.error;
  if (isPortError(error) && error.failure.kind === 'invalid_input')
    return { code: 'invalid_request', status: 0 };
  return { code: 'temporarily_unavailable', status: 0 };
}

export function monthlyFailure(
  error: unknown,
): 'unsealed' | 'not_available_in_scope' | 'unavailable' | 'error' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind !== 'http') return 'error';
  if (
    failure.error.status === 409 &&
    failure.error.detail_code === 'accreditation_monthly_package_not_available'
  )
    return 'unsealed';
  if (failure.behaviour.kind === 'not_available_in_scope') return 'not_available_in_scope';
  return 'error';
}

/** Las concesiones de SUP-U05-41 nunca pasan por React, TanStack ni almacenamiento. */
export async function downloadZone(
  plantId: string,
  period: string,
  zoneId: string,
  navigate: (url: string) => void,
) {
  const grants = await monthlyPort.download.read({ path: { plant_id: plantId, period } });
  const grant = grants.zones.find((entry) => entry.zone_id === zoneId);
  if (grant === undefined) throw new Error('No hay descarga para la zona solicitada.');
  const url = new URL(grant.url);
  if (url.protocol !== 'https:' || url.username !== '' || url.password !== '')
    throw new Error('URL de descarga no válida.');
  navigate(grant.url);
}
