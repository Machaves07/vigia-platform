/**
 * Nombres de los alcances de una asignación (VIG-305): la organización, la planta o la zona salen
 * de `GET /hierarchy`, ya acotado por el servidor al alcance de la sesión. Un alcance que la
 * jerarquía no trae se muestra como «desconocido», nunca con su identificador.
 */
import { adminPort, type Plant } from '@/ports/scr01Admin';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';

export interface ScopeDirectory {
  readonly organization: { readonly organization_id: string; readonly name: string } | null;
  readonly plants: readonly Plant[];
}

function scopeName(directory: ScopeDirectory, level: string, scopeId: string): string {
  if (level === 'organization') {
    return directory.organization?.organization_id === scopeId
      ? directory.organization.name
      : text('common.unknown');
  }
  if (level === 'plant') {
    return (
      directory.plants.find((plant) => plant.plant_id === scopeId)?.name ?? text('common.unknown')
    );
  }
  if (level === 'zone') {
    for (const plant of directory.plants) {
      const zone = plant.zones.find((item) => item.zone_id === scopeId);
      if (zone !== undefined) return `${plant.name} · ${zone.name}`;
    }
  }
  return text('common.unknown');
}

/** «rol · nivel: alcance», con etiquetas de U-02 y el nombre del alcance. */
export function assignmentText(
  directory: ScopeDirectory,
  assignment: { readonly role: string; readonly scope_level: string; readonly scope_id: string },
): string {
  return `${label('role', assignment.role)} · ${label('scope_level', assignment.scope_level)}: ${scopeName(
    directory,
    assignment.scope_level,
    assignment.scope_id,
  )}`;
}

/** Opciones de alcance de un nivel (organización de la sesión, plantas o zonas de la jerarquía). */
export function scopeOptions(
  directory: ScopeDirectory,
  level: string,
): readonly { readonly value: string; readonly label: string }[] {
  if (level === 'organization') {
    return directory.organization === null
      ? []
      : [{ value: directory.organization.organization_id, label: directory.organization.name }];
  }
  if (level === 'plant') {
    return directory.plants.map((plant) => ({ value: plant.plant_id, label: plant.name }));
  }
  if (level === 'zone') {
    return directory.plants.flatMap((plant) =>
      plant.zones.map((zone) => ({ value: zone.zone_id, label: `${plant.name} · ${zone.name}` })),
    );
  }
  return [];
}

/** La jerarquía de la sesión como directorio de alcances. */
export function useScopeDirectory(): ScopeDirectory {
  const { principal } = useUiSession();
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr01',
      operation: 'GET /hierarchy',
      scope,
      concession: principal?.concession_id ?? null,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => adminPort.hierarchy.read({}, signal),
  });
  const organization = hierarchy.data?.organization ?? principal?.organization ?? null;
  return { organization, plants: hierarchy.data?.plants ?? [] };
}
