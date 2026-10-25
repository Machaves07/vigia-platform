/** RV-07 recorre la proyección de producción y su guarda de organización. */
import { createElement } from 'react';
import fc from 'fast-check';
import { CatalogView } from '@/pages/scr04/catalog/CatalogView';
import { ComparisonView } from '@/pages/scr04/catalog/ComparisonView';
import type { Catalog } from '@/ports/scr04Catalog';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

const allowedFields = [
  'zone_id',
  'zone_code',
  'organization_id',
  'plant_id',
  'version',
  'standards',
  'standard_id',
  'family',
  'tier_policy',
  'title_es',
  'declared_text',
  'declared_by',
  'user_id',
  'display_name',
  'role',
  'effective_from',
  'predicate',
  'all_of',
  'presence',
  'signal_role',
  'value',
  'min_duration_ms',
  'thresholds',
  'review',
  'publication',
  'minimum_coverage',
  'required_count',
  'required_camera_ids',
  'clip_window',
  'pre_seconds',
  'post_seconds',
];
const comparisonVersion = readView('vigia_contracts__models__zone_catalog__Payload').map(
  (catalog) => ({
    catalog,
    single_occupancy: false,
    aggregation_window_minutes: 60,
  }),
);

export default {
  comparison: {
    screen: 'SCR-04',
    view: 'RV-07',
    data: fc.record({ before: comparisonVersion, after: comparisonVersion }),
    organizationOf: 'organization_id',
    render: (data, context) =>
      createElement(ComparisonView, {
        before: data.before as Parameters<typeof ComparisonView>[0]['before'],
        after: data.after as Parameters<typeof ComparisonView>[0]['after'],
        organizationId: context.organizationId,
        concessionOrganizationId: context.concessionOrganizationId,
        reportContractError: context.reportContractError,
      }),
    allowedFields: [
      ...allowedFields,
      'before',
      'after',
      'catalog',
      'single_occupancy',
      'aggregation_window_minutes',
    ],
  },
  catalog: {
    screen: 'SCR-04',
    view: 'RV-07',
    data: readView('vigia_contracts__models__zone_catalog__Payload'),
    organizationOf: 'organization_id',
    render: (data, context) =>
      createElement(CatalogView, {
        catalog: data as unknown as Catalog,
        version: { single_occupancy: false, aggregation_window_minutes: 60 },
        timeZone: 'America/Bogota',
        organizationId: context.organizationId,
        concessionOrganizationId: context.concessionOrganizationId,
        reportContractError: context.reportContractError,
      }),
    allowedFields,
  },
} satisfies Record<string, ViewRegistration>;
