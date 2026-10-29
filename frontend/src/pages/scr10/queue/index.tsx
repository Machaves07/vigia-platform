/**
 * IF-21: cola por plazo, proyección periódica y resolución exclusivamente individual.
 *
 * VIG-359: el panel de evidencia o de resolución se abre junto a la fila de su detección, con el
 * foco en su encabezado, y lo devuelve al botón que lo abrió al cerrarse. Si el enlace del detalle
 * del hallazgo trae una detección en el estado de la navegación (nunca en la URL), esa detección se
 * señala y recibe el foco; si ya no está en las páginas cargadas, se dice.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router';
import { PAGE_SIZE, Pager, usePaged } from '@/components/Pager/Pager';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { reviewQueueTarget } from '@/pages/shared/FindingActions';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  queueClock,
  queueFailure,
  readDetection,
  readQueue,
  reviewQueuePort,
  type Plant,
  type QueueItem,
  type ResolutionResult,
} from '@/ports/scr10ReviewQueue';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { descriptor } from '../descriptor';
import { EvidenceView } from './EvidenceView';
import { panelId, QueueThresholds, QueueView, type PanelKind } from './QueueView';
import { Resolution } from './Resolution';
import styles from './queue.module.css';

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

/**
 * El panel de una detección, en la fila siguiente a la suya (VIG-359): al abrirse recibe el foco
 * en su encabezado; «Cerrar detalle» lo devuelve al botón que lo abrió.
 */
function Panel({
  item,
  kind,
  plants,
  onClose,
  children,
}: {
  readonly item: QueueItem;
  readonly kind: PanelKind;
  readonly plants: readonly Plant[];
  readonly onClose: () => void;
  readonly children: ReactNode;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const headingId = useId();
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const plant = plants.find((p) => p.plant_id === item.plant_id);
  return (
    <section
      id={panelId(item.detection_id)}
      aria-labelledby={headingId}
      className={styles.panel}
      data-part="panel"
      data-kind={kind}
    >
      <h2 id={headingId} ref={heading} tabIndex={-1}>
        {text(
          kind === 'resolve' ? 'finding_journey.panel_resolve' : 'finding_journey.panel_evidence',
        )}
        {': '}
        {plant?.zones.find((zone) => zone.zone_id === item.zone_id)?.name ?? text('common.unknown')}
        {' · '}
        {label('predicate_family', item.family)}
      </h2>
      <TimestampWithOffset instant={item.review_due_at} timeZone={plant?.timezone ?? ''} />
      <button type="button" onClick={onClose}>
        {text('scr10.cancel')}
      </button>
      {children}
    </section>
  );
}

/** La detección que señala el enlace del hallazgo (estado de la navegación), si la hay. */
function useTarget(): string | null {
  return reviewQueueTarget(useLocation().state);
}

export default function Page() {
  const { principal } = useUiSession();
  const [pages, setPages] = useState(1);
  const [selected, setSelected] = useState<{
    item: QueueItem;
    kind: PanelKind;
  } | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const targetCell = useRef<HTMLTableCellElement>(null);
  const focusedTarget = useRef(false);
  const receipt = useRef<HTMLDivElement>(null);
  const target = useTarget();
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
  const plants = hierarchy.data?.plants ?? [];
  const paged = usePaged(query.data?.items ?? []);
  const visibleItems = paged.visible;
  // La detección señalada desde el hallazgo se ve aunque no esté en la primera página.
  const targetIndex = query.data?.items.findIndex((item) => item.detection_id === target) ?? -1;
  const targetPage = targetIndex < 0 ? null : Math.floor(targetIndex / PAGE_SIZE);
  const { setPage } = paged;
  useEffect(() => {
    if (targetPage !== null && !focusedTarget.current) setPage(targetPage);
  }, [setPage, targetPage]);
  const targetListed = query.data?.items.some((item) => item.detection_id === target) ?? false;
  const targetVisible = visibleItems.some((item) => item.detection_id === target);
  useEffect(() => {
    // La detección señalada recibe el foco una sola vez, al aparecer en la cola.
    if (targetVisible && !focusedTarget.current) {
      focusedTarget.current = true;
      targetCell.current?.focus();
    }
  }, [targetVisible]);
  useEffect(() => {
    if (written !== null) receipt.current?.focus();
  }, [written]);
  const open = (item: QueueItem, kind: PanelKind, button: HTMLButtonElement) => {
    trigger.current = button;
    setSelected({ item, kind });
  };
  const close = () => {
    setSelected(null);
    trigger.current?.focus();
  };
  const panel = selected !== null && (
    <Panel
      key={`${selected.kind}:${selected.item.detection_id}`}
      item={selected.item}
      kind={selected.kind}
      plants={plants}
      onClose={close}
    >
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
    </Panel>
  );
  return (
    <main>
      <h1>{text('scr10.title')}</h1>
      <p>{text('scr10.explanation')}</p>
      {query.data !== undefined && <QueueThresholds items={query.data.items} plants={plants} />}
      {written !== null && (
        <div ref={receipt} tabIndex={-1} data-part="receipt">
          <Receipt result={written} scope={scope} concession={concession} />
        </div>
      )}
      <RefreshIndicator
        clock={queueClock()}
        fetchedAt={query.fetchedAt}
        isFetching={query.isFetching}
        refreshPolicy="periodic_60s"
        onRefresh={() => {
          void query.refresh();
        }}
      />
      {target !== null &&
        query.data !== undefined &&
        !targetListed &&
        written?.detection_id !== target && (
          <p role="status" data-part="target_missing">
            {text('finding_journey.queue_target_missing')}
          </p>
        )}
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
            items={visibleItems}
            plants={plants}
            now={queueClock().now()}
            canResolve={canResolve}
            onResolve={(item, button) => {
              open(item, 'resolve', button);
            }}
            onEvidence={(item, button) => {
              open(item, 'evidence', button);
            }}
            open={
              selected === null
                ? null
                : { detectionId: selected.item.detection_id, kind: selected.kind }
            }
            panel={panel}
            target={target}
            targetRef={targetCell}
          />
        </ScreenState>
      )}
      {/* VIG-366: de 20 en 20; en la última página, «Siguiente» pide la página del servidor. */}
      {query.data !== undefined && query.data.items.length > 0 && (
        <Pager
          page={paged.page}
          total={query.data.items.length}
          onPage={paged.setPage}
          more={query.data.next_cursor != null}
          loading={query.isFetching}
          onLoadMore={() => {
            setPages((n) => n + 1);
          }}
          label={text('scr10.pages')}
        />
      )}
    </main>
  );
}
