import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const manifestPath = fileURLToPath(new URL('./.world.json', import.meta.url));
export const backendPath = fileURLToPath(new URL('../../../backend', import.meta.url));

export const roles = [
  'coordinator_sst',
  'plant_manager',
  'line_manager',
  'administrator',
  'copasst',
  'provider_installer',
] as const;
export type Role = (typeof roles)[number];

export interface World {
  readonly base_url: string;
  readonly store_origin: string;
  readonly users: Record<Role, { readonly email: string; readonly user_id: string }>;
  readonly credentials_file: string;
  readonly ca_file: string;
  readonly organization_id: string;
  readonly plant_ids: readonly string[];
  readonly zone_ids: readonly string[];
  readonly non_productive_zone_id: string;
  readonly concession_id: string;
  /** Canal local del intermediario de fallos entre la aplicación y la API (`faults.ts`). */
  readonly fault_control_url: string;
}

/** Archivo escrito exclusivamente por el proceso local del mundo, no por la aplicación. */
export function readWorld(): World {
  return JSON.parse(readFileSync(manifestPath, 'utf8')) as World;
}
