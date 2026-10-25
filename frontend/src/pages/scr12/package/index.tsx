/** IF-16: abrir la ruta pide el paquete una vez; ninguna lectura auditada se sondea. */
import { useState } from 'react';
import { Link, useParams } from 'react-router';
import { downloadZone, monthlyPort } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import styles from '../monthly.module.css';
import { MonthlyRefresh, MonthlyResult, useMonthlyContext, useMonthlyRead } from '../common';
import { Commitments } from './Commitments';
import { PackageView } from './PackageView';
import { Sample } from './Sample';

let openings = 0;
const navigateDownload = (url: string) => {
  window.location.assign(url);
};

function Workspace({
  zoneId,
  period,
  navigate,
}: {
  readonly zoneId: string;
  readonly period: string;
  readonly navigate: (url: string) => void;
}) {
  const context = useMonthlyContext();
  const [opening] = useState(() => ++openings);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState(false);
  const hierarchy = useMonthlyRead(context, 'GET /hierarchy', {}, (signal) =>
    monthlyPort.hierarchy.read({}, signal),
  );
  const query = useMonthlyRead(
    context,
    'GET /accreditation/monthly-packages',
    { zone_id: zoneId, period, opening },
    (signal) => monthlyPort.package.read({ query: { zone_id: zoneId, period } }, signal),
  );
  const plant = hierarchy.data?.plants.find((p) => p.zones.some((z) => z.zone_id === zoneId));
  const data = query.error === null ? query.data : undefined;
  return (
    <main className={styles.page}>
      <Link to="/zones/monthly">{text('navigation.scr12')}</Link>
      <h1>{text('navigation.scr12')}</h1>
      <p>{text('scr12.intro')}</p>
      <MonthlyRefresh query={query} />
      <MonthlyResult query={query}>
        {data !== undefined && (
          <>
            <PackageView data={data} timeZone={plant?.timezone ?? ''} />
            <Commitments
              key={data.package_id}
              context={context}
              packageId={data.package_id}
              zoneId={zoneId}
              period={period}
            />
            <Sample
              context={context}
              packageId={data.package_id}
              opening={opening}
              timeZone={plant?.timezone ?? ''}
            />
            <p>{text('scr12.download_info')}</p>
            {downloadError && <p role="alert">{text('scr12.download_error')}</p>}
            <button
              type="button"
              disabled={downloading || plant === undefined}
              onClick={() => {
                if (plant === undefined) return;
                setDownloading(true);
                setDownloadError(false);
                void downloadZone(plant.plant_id, period, zoneId, navigate)
                  .catch(() => {
                    setDownloadError(true);
                  })
                  .finally(() => {
                    setDownloading(false);
                  });
              }}
            >
              {text('scr12.download')}
            </button>
          </>
        )}
      </MonthlyResult>
    </main>
  );
}

export default function Page({
  navigate = navigateDownload,
}: {
  readonly navigate?: (url: string) => void;
}) {
  const { zone_id = '', period = '' } = useParams();
  return (
    <Workspace key={`${zone_id}:${period}`} zoneId={zone_id} period={period} navigate={navigate} />
  );
}
