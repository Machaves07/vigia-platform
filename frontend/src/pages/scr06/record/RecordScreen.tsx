/**
 * Acta de comisionamiento cerrada (IF-13 paso 7): `GET /commissioning-records/{record_id}`.
 *
 * Es una lectura auditada (`audited`, BR-APP-54 y A-6): solo se pide con la acción explícita
 * «consultar el acta», nunca al montar, ni al invalidarse ni por refresco periódico. El documento
 * legible se abre por navegación desde la vista (`RecordView`).
 */
import { useState } from 'react';

import { correlationOf } from '@/components/NoticeStack/notice';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { noticeFromError } from '@/pages/scr06/common/notices';
import { scr06CommissioningPort, type CommissioningRecord } from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';

import { RecordView } from './RecordView';
import styles from './record.module.css';

const COMMISSIONING_RECORD = 'GET /commissioning-records/{record_id}';

export interface RecordScreenProps {
  readonly recordId: string;
  readonly clock: Clock;
  readonly scope?: string;
  readonly concession?: string | null;
}

export function RecordScreen({
  recordId,
  clock,
  scope = 'all',
  concession = null,
}: RecordScreenProps) {
  const [requested, setRequested] = useState(false);
  const query = useScreenQuery<CommissioningRecord>({
    key: screenKey({
      screen: 'scr06',
      operation: COMMISSIONING_RECORD,
      scope,
      concession,
      params: { record_id: recordId },
    }),
    read: ({ signal }) =>
      scr06CommissioningPort.commissioningRecord.read({ path: { record_id: recordId } }, signal),
    policy: policyFor(COMMISSIONING_RECORD),
    enabled: requested,
  });
  const queryNotice = query.error === null ? null : noticeFromError(query.error, 'scr06-record');
  const correlationId =
    queryNotice === null ? undefined : (correlationOf(queryNotice) ?? undefined);

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('navigation.scr06')}</h1>
      {!requested ? (
        <section className={styles.panel} aria-label={text('scr06.record.title')}>
          <p className={styles.intro}>{text('scr06.record.audited_hint')}</p>
          <div>
            <button
              type="button"
              className={styles.button}
              data-part="request_record"
              onClick={() => {
                setRequested(true);
              }}
            >
              {text('scr06.record.request')}
            </button>
          </div>
        </section>
      ) : query.status === 'error' ? (
        <ScreenState
          state="error"
          {...(correlationId === undefined ? {} : { correlationId })}
          onRetry={() => {
            void query.refresh();
          }}
        />
      ) : query.data === undefined ? (
        <ScreenState state="loading" />
      ) : (
        <>
          <RecordView record={query.data} />
          <RefreshIndicator
            fetchedAt={query.fetchedAt}
            refreshPolicy="on_demand"
            isFetching={query.isFetching}
            onRefresh={() => {
              void query.refresh();
            }}
            clock={clock}
          />
        </>
      )}
    </main>
  );
}
