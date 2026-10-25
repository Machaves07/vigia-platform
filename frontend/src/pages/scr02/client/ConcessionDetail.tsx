/**
 * Detalle de una concesión del cliente (IF-23 pasos 2 y 3; VIG-322):
 *
 * - Las consultas del proveedor (`GET /concessions/{concession_id}/queries`, lectura auditada) se
 *   piden al abrir el detalle y al pulsar «Actualizar», nunca por temporizador (BR-APP-54, A-6).
 *   Se presentan como «accesos o intentos del proveedor», no como datos entregados (A-48).
 * - Revocar exige confirmación explícita. El botón se ofrece en cualquier estado: si la concesión
 *   ya está cerrada, el servidor responde `conflict` y su mensaje se ancla a la confirmación
 *   (BR-APP-43).
 */
import { useId, useState } from 'react';

import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import {
  concessionsClock,
  concessionsPort,
  readQueries,
  writeNotice,
  type ClientConcession,
} from '@/ports/scr02Concessions';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';

import { Moment, revokedByText, statusLabel, WriteNotices } from '../shared';
import { queryColumns, scopeText, type ScopeDirectory } from './ClientView';

function RevokePanel({
  concession,
  onRevoked,
}: {
  readonly concession: ClientConcession;
  readonly onRevoked: (revoked: ClientConcession) => void;
}) {
  const formId = useId();
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState(false);
  const mutation = useScreenMutation({
    write: (concessionId: string) =>
      concessionsPort.revoke.write({ path: { concession_id: concessionId } }),
    invalidates: { screen: 'scr02', resources: ['concessions'] },
  });
  const notice = writeNotice(mutation.error, formId);
  const noticeId = `${formId}-notice`;
  if (done) {
    return (
      <p role="status" data-text-key="scr02.revoke.done">
        {text('scr02.revoke.done')}
      </p>
    );
  }
  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => {
          mutation.reset();
          setConfirming(true);
        }}
      >
        {text('scr02.revoke.open')}
      </button>
    );
  }
  return (
    <div
      role="group"
      aria-label={text('scr02.revoke.open')}
      aria-describedby={
        notice?.field === 'form' ? `${formId}-question ${noticeId}` : `${formId}-question`
      }
    >
      <p id={`${formId}-question`}>{text('scr02.revoke.question')}</p>
      <WriteNotices notice={notice} error={mutation.error} anchoredId={noticeId} />
      <button
        type="button"
        disabled={mutation.status === 'pending'}
        onClick={() => {
          void mutation
            .run(concession.concession_id)
            .then((revoked) => {
              setDone(true);
              onRevoked(revoked);
            })
            .catch(() => undefined);
        }}
      >
        {text('scr02.revoke.confirm')}
      </button>
      <button
        type="button"
        onClick={() => {
          mutation.reset();
          setConfirming(false);
        }}
      >
        {text('scr02.revoke.cancel')}
      </button>
    </div>
  );
}

export function ConcessionDetail({
  concession,
  directory,
  timeZone,
  onRevoked,
  onClose,
}: {
  readonly concession: ClientConcession;
  readonly directory: ScopeDirectory;
  readonly timeZone: string;
  readonly onRevoked: () => void;
  readonly onClose: () => void;
}) {
  const titleId = useId();
  const [current, setCurrent] = useState(concession);
  return (
    <section aria-labelledby={titleId}>
      <h2 id={titleId}>{text('scr02.detail.title')}</h2>
      <dl>
        <dt>{text('scr02.col.scope')}</dt>
        <dd>{scopeText(directory, current)}</dd>
        <dt>{text('scr02.col.reason')}</dt>
        <dd>{current.reason ?? text('scr02.reason.none')}</dd>
        <dt>{text('scr02.col.granted_at')}</dt>
        <dd>
          <Moment instant={current.granted_at} timeZone={timeZone} />
        </dd>
        <dt>{text('scr02.col.expires_at')}</dt>
        <dd>
          <Moment instant={current.expires_at} timeZone={timeZone} />
        </dd>
        <dt>{text('scr02.col.status')}</dt>
        <dd data-part="status">{statusLabel(current.status)}</dd>
        <dt>{text('scr02.col.revoked_by')}</dt>
        <dd>{revokedByText(current.revoked_by_side)}</dd>
      </dl>
      <RevokePanel
        concession={current}
        onRevoked={(revoked) => {
          setCurrent(revoked);
          onRevoked();
        }}
      />
      <p>{text('scr02.queries.notice')}</p>
      <KeysetTable
        caption={text('scr02.queries.list')}
        columns={queryColumns(timeZone)}
        rowKey={(row) => row.record_id}
        clock={concessionsClock()}
        refreshPolicy="on_demand"
        emptyText={text('scr02.queries.empty')}
        fetchPage={(request, signal) =>
          readQueries(concession.concession_id, request.cursor, request.pageSize, signal)
        }
      />
      <button type="button" onClick={onClose}>
        {text('scr02.detail.close')}
      </button>
    </section>
  );
}
