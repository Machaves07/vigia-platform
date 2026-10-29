/**
 * SCR-09, grupo `loop` (IF-09; SUP-U05-02 y 07): estado del lazo, sus registros en orden y la
 * acción sobre la zona.
 *
 * Sistema visual v1 (VIG-365; S3-22 y S3-24): el lazo carga al abrir, con la consulta auditada
 * como el detalle (una lectura por apertura), y el panel «Siguiente paso» de la columna derecha da
 * una sola acción principal según el estado que da el servidor (`nextStep`, BR-APP-43): en lazo
 * abierto, comprometer la acción, que abre su formulario en esta misma pantalla; con acción o
 * vencida, evidencia y cierre; en un incerrable, la revisión en la fecha comprometida; cerrado,
 * solo la reapertura, con la franja «Cerrado y firmado» (quién, cuándo y con qué credencial). Si la
 * transición no existe, el servidor lo dice al registrar. Una sola actualización, en la cabecera.
 *
 * En un incerrable y en un cerrado, la fecha comprometida, la medida compensatoria y la firma salen
 * del expediente (lectura auditada, la misma de la pantalla de cierre), pedida con el lazo.
 */
import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router';
import { StatusChip } from '@/components/StatusChip/StatusChip';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { findingTitle } from '@/pages/shared/findingTitle';
import layout from '@/pages/shared/findingLayout.module.css';
import { LifecycleSteps } from '@/pages/shared/LifecycleSteps';
import { NextStepPanel, openRequested } from '@/pages/shared/NextStepPanel';
import { PageRefresh } from '@/pages/shared/PageRefresh';
import { readHistory } from '@/ports/scr09Closure';
import { classifyClock, classifyPort, type Action } from '@/ports/scr09Classify';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { text } from '@/texts';
import {
  LoopNav,
  plantTimeZone,
  Result,
  useAuditedRead,
  useLoopContext,
  useOpening,
  zoneName,
} from '../classify/common';
import { descriptor } from '../descriptor';
import { ActionForm } from './ActionForm';
import { closureSignature, loopAccess, unclosableCommitment } from './loopAccess';
import { LoopView } from './LoopView';
import styles from './loop.module.css';

const DETAIL: ReadOperation = 'GET /loop/findings/{finding_state_id}';
const RECORDS: ReadOperation = 'GET /ledger/records';

/** Franja del lazo cerrado: quién firmó, cuándo y con qué credencial, del último cierre. */
function ClosedBanner({
  signature,
  timeZone,
}: {
  readonly signature: ReturnType<typeof closureSignature> | undefined;
  readonly timeZone: string;
}) {
  return (
    <section
      className={styles.closed}
      aria-label={text('scr09.loop.closed_banner')}
      data-part="closed_banner"
    >
      <h2>{text('scr09.loop.closed_banner')}</h2>
      {signature === undefined ? null : signature === null ? (
        <p>{text('scr09.loop.closed_unread')}</p>
      ) : (
        <dl>
          {signature.signer !== null && (
            <>
              <dt>{text('scr09.loop.closed_by')}</dt>
              <dd>{signature.signer}</dd>
            </>
          )}
          {signature.signedAt !== null && (
            <>
              <dt>{text('scr09.loop.closed_at')}</dt>
              <dd>
                <TimestampWithOffset instant={signature.signedAt} timeZone={timeZone} />
              </dd>
            </>
          )}
          {signature.license !== null && (
            <>
              <dt>{text('scr09.loop.closed_license')}</dt>
              <dd>{signature.license}</dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}

export default function Page() {
  const context = useLoopContext();
  const opening = useOpening();
  const location = useLocation();
  const detail = useAuditedRead(context, DETAIL, opening, (signal) =>
    classifyPort.detail.read({ path: { finding_state_id: context.findingId } }, signal),
  );
  const granted = new Set(context.permissions);
  const canAssign =
    descriptor.sections
      .find((section) => section.id === 'action')
      ?.required_permissions.some((key) => granted.has(key)) ?? false;
  const data = detail.data;
  const timeZone = plantTimeZone(context, data?.plant_id);
  const unclosable = data?.lifecycle_state === 'unclosable';
  const closed = data?.lifecycle_state === 'closed';
  const records = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: RECORDS,
      scope: context.scope,
      concession: context.concession,
      params: {
        finding_state_id: context.findingId,
        plant_id: data?.plant_id ?? '',
        zone_id: data?.zone_id ?? '',
        lifecycle_state: data?.lifecycle_state ?? '',
        opening,
      },
    }),
    policy: policyFor(RECORDS),
    enabled: unclosable || closed,
    read: ({ signal }) =>
      data === undefined ? Promise.resolve([]) : readHistory(data, context.organizationId, signal),
  });
  const [saved, setSaved] = useState<Action | null>(null);
  // El formulario de la acción se abre desde el panel (o al llegar del detalle con ese paso).
  const [actionOpen, setActionOpen] = useState(() => openRequested(location.state, 'action'));
  const form = useRef<HTMLDivElement>(null);
  const offersAction = data !== undefined && loopAccess(data.lifecycle_state).includes('action');
  const showForm = canAssign && offersAction && actionOpen;
  useEffect(() => {
    if (showForm) form.current?.querySelector('select')?.focus();
  }, [showForm]);
  return (
    <main className={layout.page}>
      <LoopNav findingId={context.findingId} />
      <header className={layout.header}>
        <div className={layout.heading}>
          <h1>{text('scr09.loop.title')}</h1>
          {data !== undefined && (
            <StatusChip
              state={data.lifecycle_state}
              overdueDays={data.overdue_days}
              dueAt={data.due_at}
              now={classifyClock().now()}
            />
          )}
        </div>
        <PageRefresh
          screen="scr09"
          reads={[
            { fetchedAt: detail.fetchedAt, operation: DETAIL },
            ...(unclosable || closed ? [{ fetchedAt: records.fetchedAt, operation: RECORDS }] : []),
          ]}
        />
      </header>
      {data !== undefined && (
        <p data-part="finding_title">
          {findingTitle(data.family, zoneName(context, data.zone_id))}
        </p>
      )}
      <p>{text('scr09.classify.audited')}</p>
      {data !== undefined && <LifecycleSteps state={data.lifecycle_state} />}
      <Result query={detail}>
        {data !== undefined && (
          <div className={layout.columns}>
            <div className={layout.main}>
              {closed && (
                <ClosedBanner
                  signature={
                    // Si el expediente no se pudo leer, la franja lo dice (no queda en blanco).
                    records.data !== undefined
                      ? closureSignature(records.data)
                      : records.error !== null
                        ? null
                        : undefined
                  }
                  timeZone={timeZone}
                />
              )}
              {saved !== null && (
                <p role="status" data-part="action_saved">
                  {text('scr09.loop.action_saved')}:{' '}
                  <TimestampWithOffset instant={saved.due_at} timeZone={timeZone} dateOnly />
                </p>
              )}
              {showForm && (
                <div ref={form}>
                  <ActionForm
                    context={context}
                    plantId={data.plant_id}
                    onWritten={(action) => {
                      setSaved(action);
                      setActionOpen(false);
                      detail.request();
                    }}
                  />
                </div>
              )}
              <LoopView detail={data} zone={zoneName(context, data.zone_id)} timeZone={timeZone} />
            </div>
            <aside className={layout.aside}>
              <NextStepPanel
                findingId={context.findingId}
                state={data.lifecycle_state}
                permissions={granted}
                dueAt={data.due_at}
                detectionId={data.subject_kind === 'detection' ? data.subject_id : null}
                commitment={unclosable ? unclosableCommitment(records.data ?? []) : null}
                timeZone={timeZone}
                {...(canAssign
                  ? {
                      onAction: () => {
                        setActionOpen(true);
                      },
                    }
                  : {})}
              />
            </aside>
          </div>
        )}
      </Result>
    </main>
  );
}
