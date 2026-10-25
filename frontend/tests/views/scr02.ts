/**
 * RV-04 de SCR-02 (VIG-322) con las columnas de producción de los dos paneles:
 *
 * - Concesiones del cliente: alcance, motivo, concedida, vence, estado y quién revocó. Nunca el
 *   proveedor ni la persona del proveedor (`provider_organization_id`, `provider_user_id`), que
 *   RV-04 no lista. Sin jerarquía, el alcance se nombra «desconocido» y su identificador no se pinta.
 * - Accesos o intentos del proveedor: momento, operación y recurso. Ni `record_id`, ni la persona
 *   del proveedor, ni el motivo, ni el método.
 * - Concesiones del proveedor: cliente y alcance con el identificador que el cliente le dio,
 *   concedida, vence, estado y quién revocó.
 *
 * Ninguna lleva `organization_id` por registro: el servidor acota a la organización de la sesión
 * (la propia concesión del proveedor es un registro suyo) y PR-APP-10 no las recorre.
 */
import { createElement } from 'react';

import { clientColumns, queryColumns } from '@/pages/scr02/client/ClientView';
import { providerColumns } from '@/pages/scr02/provider/ProviderView';
import { StaticRows } from '@/pages/scr02/shared';
import type { ClientConcession, ProviderConcession, ProviderQuery } from '@/ports/scr02Concessions';

import { readView } from '../generators';
import type { ViewRegistration } from './types';

const TIME_ZONE = 'America/Bogota';

export default {
  client_concessions: {
    screen: 'SCR-02',
    view: 'RV-04',
    data: readView('ConcessionsView'),
    render: (data) =>
      createElement(StaticRows<ClientConcession>, {
        caption: 'RV-04',
        columns: clientColumns(null, TIME_ZONE, () => undefined),
        rows: data.concessions as ClientConcession[],
      }),
    allowedFields: [
      'concessions',
      'scope_level',
      'scope_id',
      'reason',
      'granted_at',
      'expires_at',
      'status',
      'revoked_by_side',
    ],
  },
  provider_queries: {
    screen: 'SCR-02',
    view: 'RV-04',
    data: readView('ProviderQueriesPage'),
    render: (data) =>
      createElement(StaticRows<ProviderQuery>, {
        caption: 'RV-04',
        columns: queryColumns(TIME_ZONE),
        rows: data.queries as ProviderQuery[],
      }),
    allowedFields: ['queries', 'occurred_at', 'operation', 'resource'],
  },
  provider_concessions: {
    screen: 'SCR-02',
    view: 'RV-04',
    data: readView('ProviderConcessionsView'),
    render: (data) =>
      createElement(StaticRows<ProviderConcession>, {
        caption: 'RV-04',
        columns: providerColumns(TIME_ZONE, () => null),
        rows: data.concessions as ProviderConcession[],
      }),
    allowedFields: [
      'concessions',
      'client_organization_id',
      'scope_level',
      'scope_id',
      'granted_at',
      'expires_at',
      'status',
      'revoked_by_side',
    ],
  },
} satisfies Record<string, ViewRegistration>;
