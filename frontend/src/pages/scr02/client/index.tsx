/**
 * SCR-02, grupo `client` (`concessions.read`, `concessions.revoke`; RV-04; IF-23; H-56): las
 * concesiones de los proveedores sobre la organización con alcance, motivo, concedida, vence,
 * estado y quién revocó; el detalle abre los accesos o intentos del proveedor a demanda y permite
 * revocar con confirmación.
 *
 * La lista no pasa por la caché de consultas (la `KeysetTable` la pide y tiene su «Actualizar»):
 * tras revocar se vuelve a montar para pedirla de nuevo. La jerarquía (`GET /hierarchy`) solo da
 * los nombres del alcance.
 */
import { useState } from 'react';

import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import {
  concessionsClock,
  concessionsPort,
  readClientConcessions,
  type ClientConcession,
} from '@/ports/scr02Concessions';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

import { browserTimeZone } from '../shared';
import { clientColumns, type ScopeDirectory } from './ClientView';
import { ConcessionDetail } from './ConcessionDetail';

function useDirectory(): ScopeDirectory {
  const { principal } = useUiSession();
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr02',
      operation: 'GET /hierarchy',
      scope: principal?.organization.organization_id ?? 'none',
      concession: principal?.concession_id ?? null,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => concessionsPort.hierarchy.read({}, signal),
  });
  return hierarchy.data ?? null;
}

export default function Page() {
  const directory = useDirectory();
  const [listRun, setListRun] = useState(0);
  const [selected, setSelected] = useState<ClientConcession | null>(null);
  const timeZone = browserTimeZone();
  return (
    <main>
      <h1>{text('scr02.client.title')}</h1>
      <p>{text('scr02.client.intro')}</p>
      <KeysetTable<ClientConcession>
        key={`list-${String(listRun)}`}
        caption={text('scr02.client.list')}
        columns={clientColumns(directory, timeZone, setSelected)}
        rowKey={(row) => row.concession_id}
        clock={concessionsClock()}
        refreshPolicy="on_demand"
        emptyText={text('scr02.client.empty')}
        fetchPage={(_request, signal) => readClientConcessions(signal)}
      />
      {selected !== null && (
        <ConcessionDetail
          key={`detail-${selected.concession_id}`}
          concession={selected}
          directory={directory}
          timeZone={timeZone}
          onRevoked={() => {
            setListRun((run) => run + 1);
          }}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </main>
  );
}
