/** IF-17: todas las operaciones de exportación pasan por este puerto. */
import type { components } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type ExportJob = components['schemas']['ExportStatusOut'];
export type ExportRequest = components['schemas']['ExportRequestBody'];
export type ExportState = components['schemas']['ExportState'];
export type Plant = components['schemas']['PlantView'];
export type Checkpoints = components['schemas']['CheckpointsOut'];
export type DocumentDownload = components['schemas']['ExportDocumentUrlOut'];
export type VolumeDownloads = components['schemas']['ExportDownloadOut'];
export type LedgerPage = components['schemas']['LedgerRecordPage'];

/** RV-18 distingue el documento portátil del documento de identidad (P3). */
export interface ExportReadView {
  readonly plant_id: string;
  readonly zone_ids: readonly string[];
  readonly period: ExportJob['period'];
  readonly package_form: ExportJob['package_form'];
  readonly export_state: ExportState;
  readonly manifest_summary: {
    readonly archive_sha256: string;
    readonly portable_sha256: string;
  } | null;
}

export function exportReadView(job: ExportJob): ExportReadView {
  return {
    plant_id: job.plant_id,
    zone_ids: job.zone_ids,
    period: job.period,
    package_form: job.package_form,
    export_state: job.export_state,
    manifest_summary:
      job.manifest_summary === null
        ? null
        : {
            archive_sha256: job.manifest_summary.archive_sha256,
            portable_sha256: job.manifest_summary.document_sha256,
          },
  };
}

export const exportsPort = defineScreenPort('SCR-14', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  gates: {
    kind: 'read',
    status: 'fixed',
    operationId: 'gate_state_zones__zone_id__gates_get',
    permission: 'catalog.read',
  },
  request: {
    kind: 'write',
    status: 'fixed',
    operationId: 'request_export_accreditation_exports_post',
    permission: 'export.create',
  },
  status: {
    kind: 'read',
    status: 'fixed',
    operationId: 'export_status_accreditation_exports__export_id__get',
    permission: 'export.create',
  },
  document: {
    kind: 'write',
    status: 'fixed',
    operationId: 'export_document_url_accreditation_exports__export_id__download_url_post',
    permission: 'export.create',
  },
  volumes: {
    kind: 'read',
    status: 'fixed',
    operationId: 'export_download_accreditation_exports__export_id__download_get',
    permission: 'export.create',
  },
  checkpoints: {
    kind: 'read',
    status: 'fixed',
    operationId: 'checkpoints_integrity_checkpoints_get',
    permission: 'integrity.verify',
  },
  verifier: {
    kind: 'read',
    status: 'fixed',
    operationId: 'verifier__well_known_vigia_verifier_get',
    permission: 'public',
  },
  // SUP-U05-22 no expone manifest_sha256: se lee export_produced, nunca archive_sha256.
  records: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_records_ledger_records_get',
    permission: 'findings.read',
  },
});

export function exportsClock() {
  return currentTransport().clock;
}

export function exportFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  if (error.failure.kind === 'offline' || error.failure.kind === 'unavailable')
    return 'unavailable';
  if (error.failure.kind === 'http' && error.failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

export function exportNotice(error: Error | null): Notice | null {
  if (!isPortError(error) || error.failure.kind !== 'http') return null;
  const failure = error.failure.error;
  const fields: Readonly<Record<string, string>> = {
    accreditation_export_zones_span_plants: 'zones',
    accreditation_export_zone_out_of_scope: 'zones',
    accreditation_export_scope_too_large: 'period',
  };
  const field = fields[failure.detail_code ?? ''];
  return {
    id: `scr14:${failure.detail_code ?? failure.code}`,
    error: failure,
    ...(field === undefined ? {} : { field }),
  };
}

export function conflictState(error: Error | null): ExportState | null {
  if (!isPortError(error) || error.failure.kind !== 'http' || error.failure.error.status !== 409)
    return null;
  const body = error.failure.error.body;
  if (typeof body !== 'object' || body === null || !('export_state' in body)) return null;
  const state = body.export_state;
  return state === 'requested' ||
    state === 'in_progress' ||
    state === 'ready' ||
    state === 'failed' ||
    state === 'expired'
    ? state
    : null;
}

/** Fechas locales de la planta convertidas por la pantalla; límite idéntico al servidor. */
export function validRequest(body: ExportRequest): boolean {
  const duration = Date.parse(body.period.to) - Date.parse(body.period.from);
  return (
    body.plant_id !== '' &&
    body.zone_ids.length > 0 &&
    body.zone_ids.length <= 20 &&
    duration > 0 &&
    duration <= 366 * 86_400_000
  );
}

/** Solo URLs web de descarga; nunca se navega a esquemas activos. */
export function downloadHref(value: string): string | undefined {
  try {
    const url = new URL(value);
    return (url.protocol === 'https:' ||
      (url.protocol === 'http:' && url.hostname === 'localhost')) &&
      url.username === '' &&
      url.password === ''
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

/** Proyección mínima del registro que prueba el hash del manifiesto de este trabajo. */
export function manifestHash(
  page: LedgerPage,
  job: ExportJob,
  organizationId: string,
): string | null {
  for (const record of page.items) {
    const content = record.content;
    if (
      record.organization_id !== organizationId ||
      record.plant_id !== job.plant_id ||
      record.record_type !== 'export_produced' ||
      typeof content !== 'object' ||
      content === null ||
      Array.isArray(content)
    )
      continue;
    if (
      'export_id' in content &&
      content.export_id === job.export_id &&
      'manifest_sha256' in content &&
      typeof content.manifest_sha256 === 'string' &&
      /^[a-f0-9]{64}$/.test(content.manifest_sha256)
    )
      return content.manifest_sha256;
  }
  return null;
}
