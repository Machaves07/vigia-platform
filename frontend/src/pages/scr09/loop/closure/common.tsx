/** Contexto propio de las tres subrutas; las lecturas del expediente son solo a demanda. */
import { useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  closureClock,
  closureNotice,
  closurePort,
  readHistory,
  type Detail,
} from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { ClosureHistory } from './History';
import styles from './Closure.module.css';

function useClosureContext() {
  const { principal } = useUiSession();
  const { finding_state_id: findingId = '' } = useParams();
  const organizationId = principal?.organization.organization_id ?? '';
  const scope = `${organizationId}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr09', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => closurePort.hierarchy.read({}, signal),
  });
  return {
    findingId,
    organizationId,
    scope,
    concession,
    principal,
    plants: hierarchy.data?.plants ?? [],
    permissions: principal?.effective_permissions ?? [],
  };
}
export type ClosureContext = ReturnType<typeof useClosureContext>;

export function Failure({ error }: { readonly error: Error | null }) {
  const notice = closureNotice(error);
  if (notice !== null) return <FieldNotice id={notice.id} notice={notice} />;
  return error === null ? null : <p role="alert">{text('screen_state.unavailable')}</p>;
}

export function ClosureScreen({
  title,
  children,
}: {
  readonly title: string;
  readonly children: (props: {
    context: ClosureContext;
    detail: Detail;
    timeZone: string;
    records: Awaited<ReturnType<typeof readHistory>>;
    onWritten: () => void;
  }) => ReactNode;
}) {
  const context = useClosureContext();
  const [requested, setRequested] = useState(false);
  const detail = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /loop/findings/{finding_state_id}',
      scope: context.scope,
      concession: context.concession,
      params: { finding_state_id: context.findingId },
    }),
    policy: policyFor('GET /loop/findings/{finding_state_id}'),
    enabled: requested,
    read: ({ signal }) =>
      closurePort.detail.read({ path: { finding_state_id: context.findingId } }, signal),
  });
  const data = detail.data;
  const records = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /ledger/records',
      scope: context.scope,
      concession: context.concession,
      params: {
        finding_state_id: context.findingId,
        plant_id: data?.plant_id ?? '',
        zone_id: data?.zone_id ?? '',
      },
    }),
    policy: policyFor('GET /ledger/records'),
    enabled: requested && data !== undefined,
    read: ({ signal }) =>
      data === undefined ? Promise.resolve([]) : readHistory(data, context.organizationId, signal),
  });
  const timeZone = context.plants.find((p) => p.plant_id === data?.plant_id)?.timezone;
  const load = () => {
    if (requested) {
      void detail.refresh();
      void records.refresh();
    } else setRequested(true);
  };
  return (
    <main className={styles.page}>
      <Link to={`/findings/${context.findingId}/loop`}>{text('scr09.closure.back')}</Link>
      <h1>{title}</h1>
      <p>{text('scr09.closure.audited')}</p>
      <button type="button" onClick={load} disabled={detail.isFetching || records.isFetching}>
        {text('scr09.closure.load')}
      </button>
      {requested && (
        <>
          <RefreshIndicator
            clock={closureClock()}
            fetchedAt={detail.fetchedAt}
            isFetching={detail.isFetching}
            refreshPolicy="on_demand"
            onRefresh={load}
          />
          <Failure error={detail.error} />
          {data === undefined && detail.error === null && <ScreenState state="loading" />}
          {data !== undefined && (
            <>
              <Failure error={records.error} />
              <ClosureHistory
                records={records.data ?? []}
                organizationId={context.organizationId}
                timeZone={timeZone ?? 'UTC'}
                canReadAttachments={context.permissions.includes('closure_attachments.read')}
              />
              {timeZone === undefined ? (
                <ScreenState state="loading" />
              ) : (
                children({
                  context,
                  detail: data,
                  timeZone,
                  records: records.data ?? [],
                  onWritten: load,
                })
              )}
            </>
          )}
        </>
      )}
    </main>
  );
}
