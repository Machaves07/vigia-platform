/**
 * SCR-05, `/zones/{zone_id}/gates` (IF-11): estado de compuertas de la zona (UI-17 con montaje,
 * uso y modo resultante), aviso informativo de planta sin política de incerrables, acuerdo vigente
 * con sus firmas, «confirmar mi firma» cuando el servidor la pide para la persona autenticada,
 * acta de alcance, acuerdo de uso, aprobación y revocación de compuerta.
 *
 * Los formularios se pintan según las claves de permiso del `Principal` (`commissioning.run`,
 * `agreements.sign`); ninguna acción se oculta ni se deshabilita por una regla de negocio propia
 * (BR-APP-43): la política, las firmas y el acta los decide el servidor.
 */
import { useState } from 'react';
import { Link } from 'react-router';
import { catalogCameras, gatesPort, type Agreement, type Confirmation } from '@/ports/scr05Gates';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import {
  findZone,
  GatesRefresh,
  GatesResult,
  PlantPolicyNotice,
  usePlantPolicy,
  useGatesContext,
} from '../shared';
import { AgreementView, GateStatePanel } from '../views';
import { AgreementForm, ApprovalSection } from './Agreement';
import { ConfirmReceipt, ConfirmSignature } from './ConfirmSignature';
import { Revocation } from './Revocation';
import { ScopeRecordWizard } from './ScopeRecordWizard';
import styles from '../scr05.module.css';

const GATES = 'GET /zones/{zone_id}/gates';
const TRANSPARENCY = 'GET /zones/{zone_id}/transparency';
const CATALOG = 'GET /zones/{zone_id}/catalog';
const SIGNATORIES = 'GET /plants/{plant_id}/signatory-policy';

export function ZoneGates({ zoneId }: { readonly zoneId: string }) {
  const context = useGatesContext();
  const { plant, zone } = findZone(context, zoneId);
  const timeZone = plant?.timezone ?? 'UTC';
  const [created, setCreated] = useState<Agreement | null>(null);
  const [receipt, setReceipt] = useState<Confirmation | null>(null);
  const zoneScope = `${context.scope}:${zoneId}`;
  const gates = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: GATES,
      scope: zoneScope,
      concession: context.concession,
      params: { zone_id: zoneId },
    }),
    policy: policyFor(GATES),
    read: ({ signal }) => gatesPort.gates.read({ path: { zone_id: zoneId } }, signal),
  });
  const transparency = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: TRANSPARENCY,
      scope: zoneScope,
      concession: context.concession,
      params: { zone_id: zoneId },
    }),
    policy: policyFor(TRANSPARENCY),
    enabled: context.can('transparency.read'),
    read: ({ signal }) => gatesPort.transparency.read({ path: { zone_id: zoneId } }, signal),
  });
  const canRun = context.can('commissioning.run');
  const catalog = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: CATALOG,
      scope: zoneScope,
      concession: context.concession,
      params: { zone_id: zoneId },
    }),
    policy: policyFor(CATALOG),
    enabled: canRun,
    read: ({ signal }) => gatesPort.catalog.read({ path: { zone_id: zoneId } }, signal),
  });
  const plantId = gates.data?.plant_id ?? plant?.plant_id;
  const plantPolicy = usePlantPolicy(context, plantId);
  const signatories = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: SIGNATORIES,
      scope: `${context.scope}:${plantId ?? ''}`,
      concession: context.concession,
      params: { plant_id: plantId ?? '' },
    }),
    policy: policyFor(SIGNATORIES),
    enabled: canRun && plantId !== undefined,
    read: ({ signal }) =>
      gatesPort.signatoryPolicy.read({ path: { plant_id: plantId ?? '' } }, signal),
  });

  const view = transparency.data;
  const pending = view?.pending_confirmation_for_me ?? null;
  const approvable = created?.status === 'pending_signatures' ? created.agreement_id : pending;
  const data = gates.data;

  return (
    <main className={styles.page}>
      <nav aria-label={text('scr05.navigation')}>
        {plant === undefined ? null : (
          <Link to={`/plants/${plant.plant_id}/gates`}>{text('scr05.back_to_plant')}</Link>
        )}
      </nav>
      <h1>{text('navigation.scr05')}</h1>
      {zone === undefined ? null : (
        <p data-part="zone">
          {text('scr05.zone')} {zone.name}
        </p>
      )}
      {plantId === undefined ? null : (
        <PlantPolicyNotice policy={plantPolicy.data} plantId={plantId} />
      )}
      <section className={styles.section} aria-labelledby="scr05-gates">
        <h2 id="scr05-gates">{text('scr05.gates.title')}</h2>
        <GatesRefresh query={gates} operation={GATES} />
        <GatesResult query={gates} empty={null}>
          {data === undefined ? null : <GateStatePanel gates={data} timeZone={timeZone} />}
        </GatesResult>
      </section>
      {data === undefined || plantId === undefined ? null : (
        <>
          <section className={styles.section} aria-labelledby="scr05-agreement">
            <h2 id="scr05-agreement">{text('scr05.agreement.title')}</h2>
            {view?.current_agreement === null || view?.current_agreement === undefined ? (
              <p>{text('scr05.agreement.none')}</p>
            ) : (
              <AgreementView
                agreement={view.current_agreement}
                timeZone={timeZone}
                caption={text('scr05.agreement.current_signatories')}
              />
            )}
            {created === null ? null : (
              <div className={styles.section} data-part="created_agreement">
                <h3>{text('scr05.agreement.created_title')}</h3>
                <AgreementView
                  agreement={created}
                  timeZone={timeZone}
                  caption={text('scr05.agreement.created_signatories')}
                />
              </div>
            )}
            {pending !== null && view !== undefined && context.can('agreements.sign') ? (
              <ConfirmSignature
                key={`confirm:${pending}`}
                agreementId={pending}
                transparency={view}
                onConfirmed={setReceipt}
              />
            ) : null}
            {receipt === null ? null : <ConfirmReceipt receipt={receipt} timeZone={timeZone} />}
            {/* Los firmantes propuestos parten de la política de la planta: se espera a leerla. */}
            {canRun && signatories.status !== 'pending' ? (
              <AgreementForm
                key={signatories.data?.updated_at ?? 'none'}
                zoneId={zoneId}
                plantId={plantId}
                signatoryPolicy={signatories.data}
                currentAgreementId={
                  data.usage.status === 'approved' ? data.usage.agreement_id : null
                }
                onCreated={setCreated}
              />
            ) : null}
            {canRun && approvable !== null ? (
              <ApprovalSection
                key={`approval:${approvable}`}
                agreementId={approvable}
                onApproved={(approval) => {
                  setCreated(approval.agreement);
                }}
              />
            ) : null}
          </section>
          {canRun ? (
            <ScopeRecordWizard
              zoneId={zoneId}
              plantId={plantId}
              cameras={catalogCameras(catalog.data)}
            />
          ) : null}
          {canRun ? <Revocation zoneId={zoneId} /> : null}
        </>
      )}
    </main>
  );
}
