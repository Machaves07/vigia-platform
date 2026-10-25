/**
 * RV-04 del proveedor (VIG-322): columnas de las concesiones que la persona se concedió (cliente,
 * alcance, concedida, vence, estado y quién revocó), compartidas por la `KeysetTable` del panel y
 * por la vista registrada para PR-APP-07. `ProviderConcessionView` no trae el motivo (vive en el
 * expediente del cliente) ni nombres: el proveedor no ve la jerarquía del cliente sin concesión,
 * así que el cliente y el alcance se muestran con el identificador que el cliente le dio.
 */
import type { ReactNode } from 'react';

import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import type { ProviderConcession } from '@/ports/scr02Concessions';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { Moment, revokedByText, statusLabel } from '../shared';

export function providerScopeText(row: Pick<ProviderConcession, 'scope_level'>): string {
  return label('scope_level', row.scope_level);
}

export function providerColumns(
  timeZone: string,
  actions?: (row: ProviderConcession) => ReactNode,
): readonly KeysetColumn<ProviderConcession>[] {
  const columns: KeysetColumn<ProviderConcession>[] = [
    {
      id: 'client',
      header: text('scr02.col.client'),
      cell: (row) => <code>{row.client_organization_id}</code>,
    },
    {
      id: 'scope',
      header: text('scr02.col.scope'),
      cell: (row) => (
        <>
          {providerScopeText(row)}: <code>{row.scope_id}</code>
        </>
      ),
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
  if (actions !== undefined) {
    columns.push({ id: 'actions', header: text('scr02.col.actions'), cell: actions });
  }
  return columns;
}
