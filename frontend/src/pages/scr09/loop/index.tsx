/**
 * SCR-09, grupo `loop` (IF-09; SUP-U05-02 y 07): estado del lazo, sus registros en orden y la
 * acción sobre la zona. Los enlaces a evidencia y cierre, incerrable, su revisión y reapertura
 * (VIG-328) y el formulario de la acción salen del estado que da el servidor y de la tabla de
 * transiciones de U-04 (`loopAccess`, VIG-363, BR-APP-43), y de las claves que concede
 * (`effective_permissions`): un lazo cerrado solo ofrece la reapertura y uno incerrable destaca la
 * revisión en la fecha comprometida. Si la transición no existe, el servidor lo dice al registrar.
 *
 * En un incerrable, la fecha comprometida y la medida compensatoria salen de su declaración en el
 * expediente (lectura auditada, la misma de la pantalla del incerrable), pedida con el lazo.
 */
import { useState } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { readHistory } from '@/ports/scr09Closure';
import { classifyPort, type Action } from '@/ports/scr09Classify';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import {
  LoopNav,
  plantTimeZone,
  Refresh,
  Result,
  useAuditedRead,
  useLoopContext,
  zoneName,
} from '../classify/common';
import { descriptor } from '../descriptor';
import { ActionForm } from './ActionForm';
import { loopAccess, unclosableCommitment } from './loopAccess';
import { LoopNext } from './LoopNext';
import { LoopView } from './LoopView';

export default function Page() {
  const context = useLoopContext();
  const operation = 'GET /loop/findings/{finding_state_id}';
  const detail = useAuditedRead(context, operation, (signal) =>
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
    enabled: detail.requested && unclosable,
    read: ({ signal }) =>
      data === undefined ? Promise.resolve([]) : readHistory(data, context.organizationId, signal),
  });
  const [saved, setSaved] = useState<Action | null>(null);
  return (
    <main>
      <LoopNav findingId={context.findingId} />
      <h1>{text('scr09.loop.title')}</h1>
      <p>{text('scr09.classify.audited')}</p>
      <button
        type="button"
        disabled={detail.isFetching}
        onClick={() => {
          detail.request();
          if (records.fetchedAt !== null) void records.refresh();
        }}
      >
        {text('scr09.loop.load')}
      </button>
      {detail.requested && (
        <>
          <Refresh query={detail} operation={operation} />
          <Result query={detail}>
            {data !== undefined && (
              <>
                <LoopView detail={data} zone={zoneName(context, data.zone_id)} timeZone={timeZone}>
                  <LoopNext
                    findingId={context.findingId}
                    state={data.lifecycle_state}
                    permissions={granted}
                    commitment={unclosable ? unclosableCommitment(records.data ?? []) : null}
                    timeZone={timeZone}
                  />
                </LoopView>
                {canAssign && loopAccess(data.lifecycle_state).includes('action') && (
                  <ActionForm
                    context={context}
                    plantId={data.plant_id}
                    onWritten={(action) => {
                      setSaved(action);
                      detail.request();
                    }}
                  />
                )}
                {saved !== null && (
                  <p role="status" data-part="action_saved">
                    {text('scr09.loop.action_saved')}:{' '}
                    <TimestampWithOffset instant={saved.due_at} timeZone={timeZone} />
                  </p>
                )}
              </>
            )}
          </Result>
        </>
      )}
    </main>
  );
}
