/** RV-03 recorre el mismo componente de producción. Las plantas no llevan organization_id. */
import { createElement } from 'react';
import { HierarchyView } from '@/pages/scr01/hierarchy/HierarchyView';
import type { Hierarchy } from '@/ports/scr01Hierarchy';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  hierarchy: {
    screen: 'SCR-01',
    view: 'RV-03',
    data: readView('HierarchyView'),
    render: (data) => createElement(HierarchyView, { data: data as Hierarchy }),
    allowedFields: [
      'plants',
      'plant_id',
      'code',
      'name',
      'country',
      'data_region',
      'timezone',
      'status',
      'zones',
      'zone_id',
      'node_id',
      'nodes',
    ],
  },
} satisfies Record<string, ViewRegistration>;
