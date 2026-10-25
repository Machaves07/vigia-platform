/** RV-07 recorre la proyección de producción y su guarda de organización. */
import { createElement } from 'react';
import { CatalogView } from '@/pages/scr04/catalog/CatalogView';
import type { Catalog } from '@/ports/scr04Catalog';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
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
    allowedFields: [
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
    ],
  },
} satisfies Record<string, ViewRegistration>;
