/** Puerto de jerarquía y ajustes de SCR-01. La autorización y la auditoría pertenecen al servidor. */
import type { components } from '@/client/schema';
import settingsSchema from '../../openapi/bodies/update_settings_organization_settings_patch.json';
import plantSchema from '../../openapi/bodies/create_plant_plants_post.json';
import configurationSchema from '../../openapi/bodies/change_configuration_loop_plants__plant_id__configuration_put.json';
import type { Notice } from '@/components/NoticeStack/notice';
import { reasonLimits } from '@/components/ReasonField/limits';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
import { checkFreeText, validateBody, type ValidationIssue } from './validation';

/** BR-APP-62: las páginas de SCR-01 leen el tipo de los avisos de validación por su propio puerto. */
export type { ValidationIssue };

export type Hierarchy = components['schemas']['HierarchyView'];
export type Plant = components['schemas']['PlantView'];
export type PlantBody = components['schemas']['PlantBody'];
export type ZoneBody = components['schemas']['ZoneBody'];
export type OrganizationSettings = components['schemas']['SettingsView'];
export type SettingsBody = Pick<
  OrganizationSettings,
  'concession_max_days' | 'concession_default_days'
>;
export type LoopConfiguration = components['schemas']['ConfigurationOut'];
export type ConfigurationChange = components['schemas']['ConfigurationChangeBody'];

export const hierarchyPort = defineScreenPort('SCR-01', {
  // La ruta de lectura de U-02 solo exige sesión; hierarchy.read protege la sección del armazón.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  createPlant: {
    kind: 'write',
    status: 'fixed',
    operationId: 'create_plant_plants_post',
    permission: 'hierarchy.manage',
  },
  createZone: {
    kind: 'write',
    status: 'fixed',
    operationId: 'create_zone_plants__plant_id__zones_post',
    permission: 'hierarchy.manage',
  },
  settings: {
    kind: 'read',
    status: 'fixed',
    operationId: 'read_settings_organization_settings_get',
    permission: 'organization.settings',
  },
  saveSettings: {
    kind: 'write',
    status: 'fixed',
    operationId: 'update_settings_organization_settings_patch',
    permission: 'organization.settings',
  },
  configuration: {
    kind: 'read',
    status: 'fixed',
    operationId: 'configuration_loop_plants__plant_id__configuration_get',
    permission: 'hierarchy.read',
  },
  saveConfiguration: {
    kind: 'write',
    status: 'fixed',
    operationId: 'change_configuration_loop_plants__plant_id__configuration_put',
    permission: 'organization.settings',
  },
});

export const CONCESSION_FIELDS = ['concession_max_days', 'concession_default_days'] as const;
export const concessionLimits = Object.fromEntries(
  CONCESSION_FIELDS.map((field) => {
    const schema = settingsSchema.body.$defs.SettingsBody.properties[field].anyOf[0];
    if (schema === undefined) throw new Error('Falta el rango generado de concesiones.');
    return [field, { min: schema.minimum, max: schema.maximum }];
  }),
) as Record<(typeof CONCESSION_FIELDS)[number], { min: number; max: number }>;

/** Solo validación estructural del espejo generado; no compara máximo y valor por defecto. */
export function settingsIssues(body: SettingsBody): readonly ValidationIssue[] {
  const verdict = validateBody('update_settings_organization_settings_patch', body);
  return verdict.ok ? [] : verdict.issues;
}

export function zoneIssues(body: ZoneBody): readonly ValidationIssue[] {
  const verdict = validateBody('create_zone_plants__plant_id__zones_post', body);
  return verdict.ok ? [] : verdict.issues;
}

export function plantIssues(body: PlantBody): readonly ValidationIssue[] {
  const verdict = validateBody('create_plant_plants_post', body);
  return verdict.ok ? [] : verdict.issues;
}

/** Dato público de despliegue, nunca elegido por el formulario ni inferido del país u otra planta. */
export function deploymentDataRegion(): string | null {
  const value = import.meta.env.VITE_PLANT_DATA_REGION;
  const schema = plantSchema.body.$defs.PlantBody.properties.data_region;
  if (typeof value !== 'string' || value.trim() === '') return null;
  const length = Array.from(value).length;
  return length >= schema.minLength && length <= schema.maxLength ? value : null;
}

export function configurationIssues(body: ConfigurationChange): readonly ValidationIssue[] {
  if (typeof body.reason !== 'string' || body.reason.trim() === '')
    return [{ field: 'reason', key: 'validation.required' }];
  const verdict = validateBody(
    'change_configuration_loop_plants__plant_id__configuration_put',
    body,
  );
  const issues = verdict.ok ? [] : [...verdict.issues];
  const freeText = checkFreeText(body.reason, configurationReasonLimits);
  if (!freeText.ok && !issues.some((issue) => issue.field === 'reason'))
    issues.push({ field: 'reason', key: freeText.key });
  return issues;
}

/** Si el dominio rechaza el motivo, el aviso del servidor prevalece dentro de ReasonField. */
export function configurationNotice(error: Error | null): Notice | undefined {
  const notice = hierarchyNotice(error);
  return notice?.error.detail_code === 'loop_configuration_change_reason_required'
    ? { ...notice, field: 'reason' }
    : undefined;
}

export function hierarchyClock() {
  return currentTransport().clock;
}

export function hierarchyFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  if (error.failure.kind === 'offline' || error.failure.kind === 'unavailable')
    return 'unavailable';
  if (error.failure.kind === 'http' && error.failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

/** El mapa cerrado decide el mensaje: detail_code prevalece sobre el texto local. */
export function hierarchyNotice(error: Error | null): Notice | null {
  if (error === null || !isPortError(error)) return null;
  if (error.failure.kind === 'http')
    return { id: 'scr01-hierarchy', error: error.failure.error, field: 'form' };
  if (error.failure.kind === 'invalid_input')
    return { id: 'scr01-hierarchy', error: { code: 'invalid_request', status: 0 }, field: 'form' };
  return {
    id: 'scr01-hierarchy',
    error: {
      code:
        error.failure.kind === 'version_outdated' ? 'version_outdated' : 'temporarily_unavailable',
      status: 0,
    },
  };
}

/** Lista explícita de parámetros leídos; nunca se serializa la respuesta recibida. */
export const LOOP_FIELDS = [
  'classification_deadline_business_days',
  'review_deadline_business_days',
  'action_deadline_default_days',
  'action_deadline_min_days',
  'action_deadline_max_days',
  'manager_highlight_overdue_days',
  'unclosable_review_default_months',
  'unclosable_review_max_months',
  'monthly_package_generation_day',
  'sample_percent',
  'sample_min',
  'sample_max',
  'adoption_silence_days',
  'exposure_coverage_threshold_percent',
  'email_grouping_hours',
  'export_max_days',
  'export_max_zones',
  'single_occupancy_default_window_minutes',
  'reminder_interval_hours',
] as const satisfies readonly (keyof LoopConfiguration)[];

export const loopLimits = Object.fromEntries(
  LOOP_FIELDS.map((field) => {
    const integer = configurationSchema.body.$defs.ConfigurationChangeBody.properties[
      field
    ].anyOf.find((branch) => branch.type === 'integer');
    if (
      integer === undefined ||
      typeof integer.minimum !== 'number' ||
      typeof integer.maximum !== 'number'
    )
      throw new Error('Falta el rango generado de configuración del lazo.');
    return [field, { min: integer.minimum, max: integer.maximum }];
  }),
) as Record<(typeof LOOP_FIELDS)[number], { min: number; max: number }>;

const reasonSchema =
  configurationSchema.body.$defs.ConfigurationChangeBody.properties.reason.anyOf.find(
    (branch) => branch.type === 'string',
  );
export const configurationReasonLimits = reasonLimits(
  { operationId: configurationSchema.operationId, body: { properties: { reason: reasonSchema } } },
  'reason',
);
