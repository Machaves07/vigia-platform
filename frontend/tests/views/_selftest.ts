/**
 * Vista de autoprueba de PR-APP-07 y PR-APP-10: solo vive en las pruebas y no entra en el
 * registro de `tests/views/` (empieza por `_`). Imita una lista de registros por zona con
 * `organization_id`: pinta el código de zona y, de cada registro de la organización del
 * `Principal` o de la concesión activa, su título y su estado; un registro de otra organización
 * no se pinta y se registra como error de contrato. `item_id` y `organization_id` llegan pero no
 * se pintan (no están en `allowedFields`).
 */
import fc from 'fast-check';
import { createElement, type ReactElement } from 'react';

import type { ViewRegistration, ViewRenderContext } from './types';

const STATUS_LABELS: Readonly<Record<string, string>> = { open: 'Abierto', closed: 'Cerrado' };

const item = fc.record({
  item_id: fc.uuid(),
  organization_id: fc.uuid(),
  title: fc.stringMatching(/^[A-Za-z][A-Za-z0-9]{9,23}$/),
  status: fc.constantFrom('open', 'closed'),
});

function records(data: Record<string, unknown>): Record<string, unknown>[] {
  const items = data.items;
  if (!Array.isArray(items)) return [];
  return items.filter(
    (entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null,
  );
}

/** Pinta la vista; `filterOrganization: false` es la mutación de PR-APP-10 (no filtra). */
export function renderSelftest(
  data: Record<string, unknown>,
  context: ViewRenderContext,
  options: { readonly filterOrganization?: boolean } = {},
): ReactElement {
  const allowed = new Set([context.organizationId, context.concessionOrganizationId]);
  const rows = records(data).flatMap((entry, index) => {
    if (options.filterOrganization !== false && !allowed.has(String(entry.organization_id))) {
      context.reportContractError({
        code: 'foreign_organization',
        field: `items[${String(index)}]`,
      });
      return [];
    }
    return [
      createElement(
        'li',
        { key: index },
        createElement('span', null, String(entry.title)),
        ' ',
        createElement('span', null, STATUS_LABELS[String(entry.status)] ?? ''),
      ),
    ];
  });
  return createElement(
    'section',
    { 'aria-label': 'Vista de autoprueba' },
    createElement('h2', null, String(data.zone_code)),
    createElement('ul', null, ...rows),
  );
}

export const selftestView = {
  screen: 'SCR-00',
  view: 'RV-00',
  data: fc.record({
    zone_code: fc.stringMatching(/^Z-[A-Z0-9]{8}$/),
    items: fc.array(item, { maxLength: 5 }),
  }),
  render: (data, context) => renderSelftest(data, context),
  allowedFields: ['zone_code', 'items', 'title', 'status'],
  organizationOf: 'organization_id',
} satisfies ViewRegistration;
