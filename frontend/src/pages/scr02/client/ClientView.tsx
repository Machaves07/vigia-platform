/**
 * RV-04 del cliente (VIG-322): columnas de las concesiones (alcance, motivo, concedida, vence,
 * estado y quién revocó) y de las consultas del proveedor (momento, operación y recurso). Las
 * comparten las `KeysetTable` de la página y la vista registrada para PR-APP-07.
 *
 * Cada consulta se presenta como «acceso o intento» y nunca como datos entregados (A-48): el
 * `provider_query` se escribe antes de atender la ruta, también cuando la ruta lo rechaza, y
 * `ProviderQueryItem` no trae ningún campo de resultado que permita distinguirlos.
 */
import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import type { ClientConcession, Hierarchy, ProviderQuery } from '@/ports/scr02Concessions';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { Moment, revokedByText, statusLabel } from '../shared';

/** Lo que se usa de `GET /hierarchy` para nombrar el alcance. */
export type ScopeDirectory = Pick<Hierarchy, 'organization' | 'plants'> | null;

/** «nivel: nombre» del alcance; un alcance que la jerarquía no trae, «desconocido». */
export function scopeText(
  directory: ScopeDirectory,
  concession: Pick<ClientConcession, 'scope_level' | 'scope_id'>,
): string {
  const level = label('scope_level', concession.scope_level);
  let name: string | undefined;
  if (concession.scope_level === 'organization') {
    name =
      directory?.organization.organization_id === concession.scope_id
        ? directory.organization.name
        : undefined;
  } else if (concession.scope_level === 'plant') {
    name = directory?.plants.find((plant) => plant.plant_id === concession.scope_id)?.name;
  }
  return `${level}: ${name ?? text('common.unknown')}`;
}

export function clientColumns(
  directory: ScopeDirectory,
  timeZone: string,
  onOpen?: (concession: ClientConcession) => void,
): readonly KeysetColumn<ClientConcession>[] {
  const columns: KeysetColumn<ClientConcession>[] = [
    { id: 'scope', header: text('scr02.col.scope'), cell: (row) => scopeText(directory, row) },
    {
      id: 'reason',
      header: text('scr02.col.reason'),
      cell: (row) => row.reason ?? text('scr02.reason.none'),
    },
    {
      id: 'granted_at',
      header: text('scr02.col.granted_at'),
      cell: (row) => <Moment instant={row.granted_at} timeZone={timeZone} />,
    },
    {
      id: 'expires_at',
      header: text('scr02.col.expires_at'),
      cell: (row) => <Moment instant={row.expires_at} timeZone={timeZone} />,
    },
    { id: 'status', header: text('scr02.col.status'), cell: (row) => statusLabel(row.status) },
    {
      id: 'revoked_by',
      header: text('scr02.col.revoked_by'),
      cell: (row) => revokedByText(row.revoked_by_side),
    },
  ];
  if (onOpen !== undefined) {
    columns.push({
      id: 'actions',
      header: text('scr02.col.actions'),
      cell: (row) => (
        <button
          type="button"
          aria-label={`${text('scr02.client.open')}: ${scopeText(directory, row)}`}
          onClick={() => {
            onOpen(row);
          }}
        >
          {text('scr02.client.open')}
        </button>
      ),
    });
  }
  return columns;
}

function operationText(operation: string): string {
  if (operation === 'read') return text('scr02.queries.operation.read');
  if (operation === 'write') return text('scr02.queries.operation.write');
  return text('common.unknown');
}

export function queryColumns(timeZone: string): readonly KeysetColumn<ProviderQuery>[] {
  return [
    {
      id: 'moment',
      header: text('scr02.queries.col_moment'),
      cell: (row) => <Moment instant={row.occurred_at} timeZone={timeZone} />,
    },
    {
      id: 'operation',
      header: text('scr02.queries.col_operation'),
      cell: (row) => operationText(row.operation),
    },
    {
      id: 'resource',
      header: text('scr02.queries.col_resource'),
      cell: (row) => <code>{row.resource}</code>,
    },
    {
      id: 'kind',
      header: text('scr02.queries.col_kind'),
      cell: () => (
        <span data-text-key="scr02.queries.kind_attempt">{text('scr02.queries.kind_attempt')}</span>
      ),
    },
  ];
}
