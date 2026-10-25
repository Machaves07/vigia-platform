/** IF-21: cola por plazo, proyección periódica y resolución exclusivamente individual. */
import { useState } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  queueClock,
  queueFailure,
  readDetection,
  readQueue,
  reviewQueuePort,
  type QueueItem,
  type ResolutionResult,
} from '@/ports/scr10ReviewQueue';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { descriptor } from '../descriptor';
import { EvidenceView } from './EvidenceView';
import { QueueView } from './QueueView';
import { Resolution } from './Resolution';

/** El POST no devuelve rol: se lee el registro creado, sin volver a enviar la decisión. */
function Receipt({
  result,
  scope,
  concession,
}: {
  readonly result: ResolutionResult;
  readonly scope: string;
  readonly concession: string | null;
}) {
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr10',
      operation: 'GET /ledger/records/{record_id}',
      scope,
      concession,
      params: { record_id: result.record_id },
    }),
    policy: policyFor('GET /ledger/records/{record_id}'),
    enabled: true,
    read: ({ signal }) =>
      reviewQueuePort.receipt.read({ path: { record_id: result.record_id } }, signal),
  });
  return (
    <>
      <p role="status">
        {text('scr10.success')}: {label('review_resolution_outcome', result.outcome)}
      </p>
      {query.data !== undefined ? (
        <WriteReceipt record={text('scr10.success')} roleInUse={query.data.actor.role_in_use} />
      ) : query.error !== null ? (
        <ScreenState
          state="error"
          onRetry={() => {
            void query.refresh();
          }}
        />
      ) : (
        <ScreenState state="loading" />
      )}
    </>
  );
}

function Evidence({
  item,
  scope,
  concession,
  organizationId,
  canViewClips,
}: {
  readonly item: QueueItem;
  readonly scope: string;
  readonly concession: string | null;
  readonly organizationId: string;
  readonly canViewClips: boolean;
}) {
  // Este componente solo se monta tras pulsar abrir evidencia; nunca desde el sondeo de la cola.
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr10',
      operation: 'GET /ledger/records',
      scope,
      concession,
      params: { detection_id: item.detection_id, received_at: item.received_at },
    }),
    policy: policyFor('GET /ledger/records'),
    enabled: true,
    read: ({ signal }) => readDetection(item, organizationId, signal),
  });
  return (
    <section aria-label={text('scr10.evidence_title')}>
      <h2>{text('scr10.evidence_title')}</h2>
      <RefreshIndicator
        clock={queueClock()}
        fetchedAt={query.fetchedAt}
        isFetching={query.isFetching}
        refreshPolicy="on_demand"
        onRefresh={() => {
          void query.refresh();
        }}
      />
      {query.error !== null ? (
        <ScreenState
          state="error"
          onRetry={() => {
            void query.refresh();
          }}
        />
      ) : query.data === undefined ? (
        <ScreenState state="loading" />
      ) : query.data === null ? (
        <p>{text('scr10.evidence_missing')}</p>
      ) : (
        <EvidenceView data={query.data} canViewClips={canViewClips} clock={queueClock()} />
      )}
    </section>
  );
}

export default function Page() {
  const { principal } = useUiSession();
  const [pages, setPages] = useState(1);
  const [selected, setSelected] = useState<{
    item: QueueItem;
    kind: 'resolve' | 'evidence';
  } | null>(null);
  const [written, setWritten] = useState<ResolutionResult | null>(null);
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const concession = principal?.concession_id ?? null;
  const granted = new Set(principal?.effective_permissions ?? []);
  const canResolve =
    descriptor.sections
      .find((s) => s.id === 'resolve')
      ?.required_permissions.some((key) => granted.has(key)) ?? false;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr10', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => reviewQueuePort.hierarchy.read({}, signal),
  });
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr10',
      operation: 'GET /loop/review-queue',
      scope,
      concession,
      params: { pages },
    }),
    policy: policyFor('GET /loop/review-queue'),
    read: ({ signal }) => readQueue(pages, signal),
  });
  const failure = query.error === null ? null : queueFailure(query.error);
  return (
    <main>
      <h1>{text('scr10.title')}</h1>
      <p>{text('scr10.explanation')}</p>
      <p>{text('scr10.confidence_context')}</p>
      {written !== null && <Receipt result={written} scope={scope} concession={concession} />}
      <RefreshIndicator
        clock={queueClock()}
        fetchedAt={query.fetchedAt}
        isFetching={query.isFetching}
        refreshPolicy="periodic_60s"
        onRefresh={() => {
          void query.refresh();
        }}
      />
      {failure !== null ? (
        failure === 'error' ? (
          <ScreenState
            state="error"
            onRetry={() => {
              void query.refresh();
            }}
          />
        ) : (
          <ScreenState state={failure} />
        )
      ) : query.data === undefined ? (
        <ScreenState state="loading" />
      ) : query.data.items.length === 0 ? (
        <ScreenState state="empty" message={text('scr10.empty')} />
      ) : (
        <ScreenState state="ready">
          <QueueView
            items={query.data.items}
            plants={hierarchy.data?.plants ?? []}
            now={queueClock().now()}
            canResolve={canResolve}
            onResolve={(item) => {
              setSelected({ item, kind: 'resolve' });
            }}
            onEvidence={(item) => {
              setSelected({ item, kind: 'evidence' });
            }}
          />
        </ScreenState>
      )}
      {query.data?.next_cursor != null && (
        <button
          type="button"
          disabled={query.isFetching}
          onClick={() => {
            setPages((n) => n + 1);
          }}
        >
          {text('scr10.more')}
        </button>
      )}
      {selected !== null && (
        <div key={`${selected.kind}:${selected.item.detection_id}`}>
          <h2>
            {hierarchy.data?.plants
              .flatMap((p) => p.zones)
              .find((zone) => zone.zone_id === selected.item.zone_id)?.name ??
              text('common.unknown')}
            {' · '}
            {label('predicate_family', selected.item.family)}
          </h2>
          <TimestampWithOffset
            instant={selected.item.review_due_at}
            timeZone={
              hierarchy.data?.plants.find((p) => p.plant_id === selected.item.plant_id)?.timezone ??
              ''
            }
          />
          <button
            type="button"
            onClick={() => {
              setSelected(null);
            }}
          >
            {text('scr10.cancel')}
          </button>
          {selected.kind === 'resolve' ? (
            canResolve && (
              <Resolution
                item={selected.item}
                onWritten={(outcome) => {
                  setWritten(outcome);
                  setSelected(null);
                }}
              />
            )
          ) : (
            <Evidence
              item={selected.item}
              scope={scope}
              concession={concession}
              organizationId={principal?.organization.organization_id ?? ''}
              canViewClips={granted.has('evidence.read')}
            />
          )}
        </div>
      )}
    </main>
  );
}
