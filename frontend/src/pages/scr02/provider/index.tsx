/**
 * SCR-02, grupo `provider` (`concessions.grant`; RV-04; IF-06; BR-APP-51, 67 a 69; A-8): el
 * inicio del usuario del proveedor. Lista sus concesiones activas, vencidas y revocadas; solicita
 * una nueva con la vista previa «el cliente verá»; y «operar bajo esta concesión» activa
 * `ActiveConcession`, que desde entonces añade `X-Vigia-Concession` a toda petición y muestra el
 * aviso permanente (UI-04). Tras activarla se vuelve a pedir `GET /me`, que bajo la concesión
 * devuelve los permisos con que se reconstruye la navegación.
 *
 * «Operar» se ofrece solo en las concesiones activas: es la máquina de estados local de
 * `ActiveConcession` (no activa una vencida ni con otra ya activa; el cambio se hace desde el
 * aviso, BR-APP-68), no una regla de autorización.
 */
import { useState, useSyncExternalStore } from 'react';

import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import {
  concessionsClock,
  readProviderConcessions,
  type ProviderConcession,
} from '@/ports/scr02Concessions';
import { useConcessionStore } from '@/state/concession';
import { useSession } from '@/state/session';
import { text } from '@/texts';

import { browserTimeZone } from '../shared';
import { providerColumns, providerScopeText } from './ProviderView';
import { RequestForm } from './RequestForm';

const noSubscription = () => () => undefined;

export default function Page() {
  const session = useSession();
  const store = useConcessionStore();
  const activeId = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    () => store?.getSnapshot().active?.concession_id ?? null,
  );
  const [listRun, setListRun] = useState(0);
  const [requesting, setRequesting] = useState(false);
  const [granted, setGranted] = useState(false);
  const [refused, setRefused] = useState(false);
  const timeZone = browserTimeZone();

  const operate = (row: ProviderConcession) => {
    const ok = store?.activate(row) ?? false;
    setRefused(!ok);
    if (ok) void session.load().catch(() => undefined);
  };

  const actions = (row: ProviderConcession) => {
    if (row.concession_id === activeId) {
      return <span role="status">{text('scr02.provider.operating')}</span>;
    }
    if (row.status !== 'active') return null;
    return (
      <button
        type="button"
        aria-label={`${text('scr02.provider.operate')}: ${providerScopeText(row)} ${row.scope_id}`}
        onClick={() => {
          operate(row);
        }}
      >
        {text('scr02.provider.operate')}
      </button>
    );
  };

  return (
    <main>
      <h1>{text('scr02.provider.title')}</h1>
      <p>{text('scr02.provider.intro')}</p>
      {refused && (
        <p role="alert" data-text-key="scr02.provider.operate_refused">
          {text('scr02.provider.operate_refused')}
        </p>
      )}
      {granted && (
        <p role="status" data-text-key="scr02.request.done">
          {text('scr02.request.done')}
        </p>
      )}
      {requesting ? (
        <RequestForm
          onGranted={() => {
            setRequesting(false);
            setGranted(true);
            setListRun((run) => run + 1);
          }}
          onCancel={() => {
            setRequesting(false);
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => {
            setGranted(false);
            setRequesting(true);
          }}
        >
          {text('scr02.request.open')}
        </button>
      )}
      <KeysetTable<ProviderConcession>
        key={`list-${String(listRun)}`}
        caption={text('scr02.provider.list')}
        columns={providerColumns(timeZone, actions)}
        rowKey={(row) => row.concession_id}
        clock={concessionsClock()}
        refreshPolicy="on_demand"
        emptyText={text('scr02.provider.empty')}
        fetchPage={(_request, signal) => readProviderConcessions(signal)}
      />
    </main>
  );
}
