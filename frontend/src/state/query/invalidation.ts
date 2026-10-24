/**
 * Invalidación por recurso tras cada escritura (BR-APP-55, PR-APP-16): escritura → recursos que
 * cambia → pantallas que muestran alguno de esos recursos. Una escritura invalida las entradas de
 * su pantalla y de esas pantallas, **y de ninguna otra**; si cambia roles, licencia o aviso del
 * propio usuario, invalida además `GET /me` (renueva el `Principal`).
 *
 * Cada mutación declara sus recursos (`useScreenMutation({ write, invalidates })`); el mapa de
 * pantallas está aquí para que una pantalla nueva o un recurso nuevo se revise en un solo sitio.
 */
import type { Screen } from './keys';

export const RESOURCES = [
  'principal',
  'hierarchy',
  'users',
  'sessions',
  'organization_settings',
  'concessions',
  'ledger_records',
  'audit_entries',
  'labels',
  'coverage',
  'integrity',
  'catalog',
  'admissions',
  'regression',
  'gates',
  'policy',
  'signatories',
  'agreements',
  'walk_tests',
  'commissioning_records',
  'nodes',
  'fleet_alarms',
  'fleet_thresholds',
  'findings',
  'classifications',
  'actions',
  'unclosable',
  'review_queue',
  'root_cause',
  'loop_configuration',
  'vocabulary',
  'exports',
  'monthly_packages',
  'commitments',
  'metrics',
  'notifications',
  'notification_preferences',
  'transparency',
] as const;

export type Resource = (typeof RESOURCES)[number];

/** Pantalla → recursos que muestra. */
export type ScreenGraph = Readonly<Record<Screen, readonly Resource[]>>;

export const SCREEN_RESOURCES: ScreenGraph = {
  shell: ['principal', 'hierarchy', 'notifications'],
  home: ['gates', 'coverage', 'findings', 'nodes'],
  scr01: ['principal', 'users', 'sessions', 'organization_settings', 'hierarchy'],
  scr02: ['concessions'],
  scr03: ['integrity', 'coverage', 'ledger_records', 'audit_entries'],
  scr04: ['catalog', 'admissions', 'regression', 'vocabulary'],
  scr05: ['gates', 'agreements', 'policy', 'signatories'],
  scr06: ['walk_tests', 'commissioning_records', 'gates', 'catalog', 'nodes'],
  scr07: ['nodes', 'fleet_alarms', 'fleet_thresholds'],
  scr08: ['findings', 'labels', 'classifications', 'actions'],
  scr09: ['findings', 'classifications', 'actions', 'unclosable', 'loop_configuration'],
  scr10: ['review_queue', 'findings'],
  scr11: ['root_cause', 'findings'],
  scr12: ['monthly_packages', 'commitments'],
  scr13: ['metrics', 'commitments', 'coverage'],
  scr14: ['exports'],
  scr15: ['notifications', 'notification_preferences'],
  scr16: ['transparency', 'gates'],
};

/** Lo que invalida una escritura. */
export interface Invalidation {
  /** Pantalla desde la que se escribe. */
  readonly screen: Screen;
  /** Recursos que la escritura cambia. */
  readonly resources: readonly Resource[];
  /** Cambia roles, licencia o aviso del propio usuario: renueva `GET /me`. */
  readonly affectsSelf?: boolean;
}

export interface InvalidationTargets {
  readonly screens: ReadonlySet<Screen>;
  readonly principal: boolean;
}

/** Pantallas cuyas entradas invalida una escritura, según el grafo pantalla → recurso. */
export function invalidationTargets(
  graph: ScreenGraph,
  invalidation: Invalidation,
): InvalidationTargets {
  const changed = new Set<Resource>(invalidation.resources);
  const screens = new Set<Screen>([invalidation.screen]);
  for (const [screen, shown] of Object.entries(graph) as [Screen, readonly Resource[]][]) {
    if (shown.some((resource) => changed.has(resource))) screens.add(screen);
  }
  return { screens, principal: invalidation.affectsSelf === true };
}
