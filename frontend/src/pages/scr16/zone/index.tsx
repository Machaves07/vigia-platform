/**
 * SCR-16, grupo `zone`: `/transparency/{zone_id}` (IF-19). La vista de transparencia de la zona,
 * «confirmar mi firma» cuando el servidor la pide y la vista en vivo con su explicación previa.
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router';
import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { transparencyClock, transparencyPort, type Confirmation } from '@/ports/scr16Transparency';
import { ScreenState } from '@/pages/shared/ScreenState';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { TransparencyRefresh, TransparencyResult, useTransparencyContext } from '../shared';
import { ConfirmReceipt, ConfirmSignature } from './ConfirmSignature';
import { TransparencyView } from './TransparencyView';
import styles from '../scr16.module.css';

const OPERATION = 'GET /zones/{zone_id}/transparency';

function ZoneTransparency({ zoneId }: { readonly zoneId: string }) {
  const context = useTransparencyContext();
  const [receipt, setReceipt] = useState<Confirmation | null>(null);
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr16',
      operation: OPERATION,
      scope: `${context.scope}:${zoneId}`,
      concession: context.concession,
      params: { zone_id: zoneId },
    }),
    policy: policyFor(OPERATION),
    read: ({ signal }) => transparencyPort.transparency.read({ path: { zone_id: zoneId } }, signal),
  });
  const plant = context.plants.find((entry) => entry.zones.some((zone) => zone.zone_id === zoneId));
  const zone = plant?.zones.find((entry) => entry.zone_id === zoneId);
  const timeZone = plant?.timezone ?? '';
  const data = query.data;
  const pending = data?.pending_confirmation_for_me ?? null;
  return (
    <main className={styles.page}>
      <nav aria-label={text('scr16.entry.zones')}>
        <Link className={styles.backLink} to="/transparency">
          {text('scr16.entry.back')}
        </Link>
      </nav>
      <h1>{text('navigation.scr16')}</h1>
      {zone === undefined ? null : (
        <p data-part="zone">
          {text('scr16.zone')} {zone.name}
        </p>
      )}
      <p>{text('scr16.intro')}</p>
      <TransparencyRefresh query={query} operation={OPERATION} />
      <TransparencyResult query={query} empty={null}>
        {data === undefined ? null : (
          <TransparencyView
            transparency={data}
            timeZone={timeZone}
            confirmation={
              <>
                {pending === null ? null : (
                  <ConfirmSignature
                    key={pending}
                    agreementId={pending}
                    transparency={data}
                    onConfirmed={setReceipt}
                  />
                )}
                {receipt === null ? null : <ConfirmReceipt receipt={receipt} timeZone={timeZone} />}
              </>
            }
            liveView={
              <section className={styles.section} aria-label={text('live_view.title')}>
                <p>{text('scr16.live_view.intro')}</p>
                <LiveViewLauncher zone_id={zoneId} clock={transparencyClock()} />
              </section>
            }
          />
        )}
      </TransparencyResult>
    </main>
  );
}

export default function Page() {
  const { zone_id } = useParams();
  if (zone_id === undefined) return <ScreenState state="not_available_in_scope" />;
  return <ZoneTransparency key={zone_id} zoneId={zone_id} />;
}
