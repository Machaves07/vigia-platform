/** La navegación no lee cobertura: cada petición parte de un clic o del selector de periodo. */
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { ScreenState } from '@/pages/shared/ScreenState';
import { isPortError } from '@/ports/base';
import { belongsToOrganization, integrityClock, integrityPort } from '@/ports/scr03Integrity';
import {
  DEFAULT_PERIOD_MS,
  composeCoverage,
  loadCoverage,
  periodEndingAt,
  periodQuery,
  type CoveragePeriod,
} from '@/state/coverage/compose';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { IntegrityQueryState, IntegrityRefresh, useIntegrityContext } from '../shared';
import styles from '../scr03.module.css';
import { CoverageView } from '../CoverageView';
import { useUiSession } from '@/state/session';

function ZoneCoverage({ zoneId }: { readonly zoneId: string }) {
  const context = useIntegrityContext();
  const navigate = useNavigate();
  const [period, setPeriod] = useState(() =>
    periodEndingAt(integrityClock().now(), DEFAULT_PERIOD_MS),
  );
  const [requested, setRequested] = useState(false);
  const plant = context.plants.find((entry) => entry.zones.some((zone) => zone.zone_id === zoneId));
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr03',
      operation: 'GET /zones/{zone_id}/coverage',
      scope: context.organizationId,
      concession: context.concession,
      params: { zone_id: zoneId, ...periodQuery(period) },
    }),
    policy: policyFor('GET /zones/{zone_id}/coverage'),
    enabled: requested,
    read: async ({ signal }) => {
      const failure: { error: Error | null } = { error: null };
      const composite = await loadCoverage({
        period,
        signal,
        clock: integrityClock(),
        fetchChunk: async (chunk, chunkSignal) => {
          const data = await integrityPort.coverage
            .read({ path: { zone_id: zoneId }, query: periodQuery(chunk) }, chunkSignal)
            .catch((error: unknown) => {
              if (
                isPortError(error) &&
                error.failure.kind === 'http' &&
                error.failure.behaviour.kind === 'not_available_in_scope'
              )
              failure.error = error;
              throw error;
            });
          if (
            !belongsToOrganization(data.organization_id, context) ||
            data.zone_id !== zoneId ||
            (plant !== undefined && data.plant_id !== plant.plant_id)
          )
            throw new Error('La cobertura no corresponde al alcance solicitado.');
          return data;
        },
      });
      if (failure.error !== null) throw failure.error;
      return composite;
    },
  });
  const empty = composeCoverage(period, []);
  const changePeriod = (next: CoveragePeriod) => {
    setPeriod(next);
    setRequested(true);
  };
  return (
    <main className={styles.page}>
      <Link to="/integrity">{text('scr03.integrity')}</Link>
      <h1>{text('scr03.coverage')}</h1>
      <label>
        {text('scr03.zone')}
        <select
          value={zoneId}
          onChange={(event) => {
            void navigate(`/zones/${event.target.value}/coverage`);
          }}
        >
          {!context.plants.some((entry) => entry.zones.some((zone) => zone.zone_id === zoneId)) && (
            <option value={zoneId}>{text('scr03.choose_zone')}</option>
          )}
          {context.plants.flatMap((entry) =>
            entry.zones.map((zone) => (
              <option key={zone.zone_id} value={zone.zone_id}>
                {entry.name}: {zone.name}
              </option>
            )),
          )}
        </select>
      </label>
      <IntegrityRefresh query={query} />
      <IntegrityQueryState query={query}>{null}</IntegrityQueryState>
      {/* El selector existe antes de la primera consulta; un compuesto incompleto nunca afirma cobertura. */}
      <CoverageView
        data={{
          organization_id: context.concessionOrganizationId ?? context.organizationId,
          composite: query.error === null && !query.isFetching ? (query.data ?? empty) : empty,
        }}
        organizationId={context.organizationId}
        concessionOrganizationId={context.concessionOrganizationId}
        timeZone={plant?.timezone ?? 'UTC'}
        clock={integrityClock()}
        onPeriodChange={changePeriod}
      />
    </main>
  );
}

export default function Page() {
  const { principal } = useUiSession();
  const { zone_id } = useParams();
  return zone_id === undefined ? (
    <ScreenState state="not_available_in_scope" />
  ) : (
    <ZoneCoverage
      key={`${zone_id}:${principal?.organization.organization_id ?? ''}:${principal?.concession_id ?? ''}`}
      zoneId={zone_id}
    />
  );
}
