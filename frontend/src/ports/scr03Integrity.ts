/** Puerto de SCR-03 (IF-18): ninguna lectura del expediente sale de este módulo. */
import type { components } from '@/client/schema';
import verifierAsset from '../../../backend/tools/vigia_verify.py?url';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type IntegrityResult = components['schemas']['ResultOut'];
export type Checkpoints = components['schemas']['CheckpointsOut'];
export type CheckpointKeys = components['schemas']['CheckpointKeysOut'];
export type VerificationAccepted = components['schemas']['VerificationAccepted'];
export type VerifyRequest = components['schemas']['VerifyRequest'];
export type CoverageResponse = components['schemas']['CoverageTimelineOut'];
export type Plant = components['schemas']['PlantView'];

export const integrityPort = defineScreenPort('SCR-03', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  verify: {
    kind: 'write',
    status: 'fixed',
    operationId: 'verify_integrity_verify_post',
    permission: 'integrity.verify',
  },
  results: {
    kind: 'read',
    status: 'fixed',
    operationId: 'results_integrity_results_get',
    permission: 'integrity.verify',
  },
  checkpoints: {
    kind: 'read',
    status: 'fixed',
    operationId: 'checkpoints_integrity_checkpoints_get',
    permission: 'integrity.verify',
  },
  keys: {
    kind: 'read',
    status: 'fixed',
    operationId: 'checkpoint_keys__well_known_vigia_checkpoint_keys_get',
    permission: 'public',
  },
  verifier: {
    kind: 'read',
    status: 'fixed',
    operationId: 'verifier__well_known_vigia_verifier_get',
    permission: 'public',
  },
  coverage: {
    kind: 'read',
    status: 'fixed',
    operationId: 'timeline_zones__zone_id__coverage_get',
    permission: 'coverage.read',
  },
});

/** Bytes originales del verificador, publicados como recurso del mismo build que la API. */
export const verifierDownload = { href: verifierAsset, name: 'vigia_verify.py' } as const;
export function integrityClock() {
  return currentTransport().clock;
}

export function integrityFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  if (error.failure.kind === 'offline' || error.failure.kind === 'unavailable')
    return 'unavailable';
  if (error.failure.kind === 'http' && error.failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

/** La API decide el alcance; una organización contradictoria es un error de contrato (BR-APP-30). */
export function belongsToOrganization(
  organization: string,
  context: {
    readonly organizationId: string;
    readonly concessionOrganizationId?: string | null;
    readonly reportContractError?: (error: { code: string }) => void;
  },
): boolean {
  if (organization === context.organizationId || organization === context.concessionOrganizationId)
    return true;
  context.reportContractError?.({ code: 'foreign_organization' });
  return false;
}
