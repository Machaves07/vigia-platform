/** RV-11 recorre los componentes de producción, incluidos los chips de zona no productiva. */
import { createElement } from 'react';
import { MemoryRouter } from 'react-router';
import fc from 'fast-check';
import { FindingsView } from '@/pages/scr08/inbox/FindingsView';
import { ZoneCard } from '@/pages/scr08/home/HomeZones';
import type { Gate, InboxPage, Zone, ZoneGate } from '@/ports/scr08Findings';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  inbox: {
    screen: 'SCR-08',
    view: 'RV-11',
    data: readView('InboxPageOut'),
    render: (data) => {
      const page = data as unknown as InboxPage;
      return createElement(
        MemoryRouter,
        null,
        createElement(FindingsView, {
          page,
          plants: [],
          gates: page.items.map((item): ZoneGate => ({
            zone_id: item.zone_id,
            regression: 'current',
            gate: {
              zone_id: item.zone_id,
              plant_id: item.plant_id,
              resulting_mode: 'productive',
              issued_at: null,
              mounting: { status: 'approved', decided_at: null, decided_by: null, record_id: null },
              usage: { status: 'approved', decided_at: null, decided_by: null, agreement_id: null },
            },
          })),
        }),
      );
    },
    allowedFields: [
      'items',
      'zones',
      'partitions',
      'next_cursor',
      'finding_state_id',
      'plant_id',
      'zone_id',
      'family',
      'tier',
      'standard',
      'version',
      'lifecycle_state',
      'age_days',
      'observability_state_at_event',
      'occurred_at',
      'due_at',
      'time_aggregated',
      'notices',
      'code',
      'since',
      'notice',
    ],
  },
  homeZone: {
    screen: 'SCR-08',
    view: 'RV-11',
    data: fc.record({ zone: readView('ZoneView'), gate: readView('GateStateOut') }),
    render: (data) =>
      createElement(
        MemoryRouter,
        null,
        createElement(ZoneCard, {
          zone: data.zone as Zone,
          gate: data.gate as Gate,
          regression: 'current',
          coverage: null,
          timeZone: 'America/Bogota',
        }),
      ),
    allowedFields: [
      'zone',
      'gate',
      'name',
      'usage',
      'status',
      'mounting',
      'resulting_mode',
      'agreement_id',
      'decided_at',
    ],
  },
} satisfies Record<string, ViewRegistration>;
