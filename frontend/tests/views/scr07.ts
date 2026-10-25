/** RV-10 usa el componente de producción, no una réplica para las propiedades. */
import { createElement } from 'react';
import { MemoryRouter } from 'react-router';
import type { FleetNode } from '@/ports/scr07Fleet';
import { FleetView } from '@/pages/scr07/inventory/FleetView';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  fleet: {
    screen: 'SCR-07',
    view: 'RV-10',
    data: readView('NodeInventoryPage'),
    render: (data) =>
      createElement(
        MemoryRouter,
        null,
        createElement(FleetView, { nodes: data.nodes as FleetNode[], plants: [] }),
      ),
    allowedFields: [
      'nodes',
      'node_id',
      'code',
      'plant_id',
      'zones',
      'software_version',
      'contract_version',
      'model_version',
      'last_heartbeat_at',
      'communication_state',
      'since',
      'local_queue',
      'pending',
      'oldest_pending_at',
      'dead_letter',
      'clock',
      'offset_ms',
      'cameras',
      'camera_id',
      'connected',
      'measured_fps',
      'declared_min_fps',
      'observability_state',
      'zone_states',
      'zone_id',
      'catalog_version',
      'coverage_ok',
      'warnings',
      'target_version',
      'last_update_result',
      'live_view_local_url',
    ],
    // NodeInventoryView no lleva organization_id: el servidor acota por sesión/concesión.
  },
} satisfies Record<string, ViewRegistration>;
