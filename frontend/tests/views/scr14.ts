/** RV-18 usa las mismas proyecciones explícitas que la pantalla. */
import { createElement } from 'react';
import { CheckpointsView, ExportView } from '@/pages/scr14/ExportView';
import {
  exportReadView,
  type Checkpoints,
  type ExportJob,
  type ExportReadView,
} from '@/ports/scr14Exports';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  exports: {
    screen: 'SCR-14',
    view: 'RV-18',
    data: readView('ExportStatusOut').map((data) => ({
      ...exportReadView(data as unknown as ExportJob),
    })),
    render: (data, context) =>
      createElement(ExportView, {
        job: data as unknown as ExportReadView,
        organizationId: context.organizationId,
        timeZone: 'UTC',
      }),
    allowedFields: [
      'plant_id',
      'zone_ids',
      'period',
      'from',
      'to',
      'package_form',
      'export_state',
      'manifest_summary',
      'archive_sha256',
      'portable_sha256',
    ],
    // ExportStatusOut no lleva organización: se usa la de la sesión/jerarquía concedida.
  },
  checkpoints: {
    screen: 'SCR-14',
    view: 'RV-18',
    data: readView('CheckpointsOut'),
    render: (data, context) =>
      createElement(CheckpointsView, {
        data: data as unknown as Checkpoints,
        organizationId: context.organizationId,
        concessionOrganizationId: context.concessionOrganizationId,
        reportContractError: context.reportContractError,
      }),
    allowedFields: ['checkpoints', 'chain', 'plant_id', 'sequence', 'entry_hash'],
    organizationOf: 'organization_id',
  },
} satisfies Record<string, ViewRegistration>;
