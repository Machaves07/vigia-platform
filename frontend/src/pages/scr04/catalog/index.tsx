/** Catálogo de planta compuesto por zonas; solo el puerto de SCR-04 accede a la API. */
import { useState } from 'react';
import { Link, useMatch, useParams } from 'react-router';
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import { catalogPayload, catalogPort, type Plant, type Standard } from '@/ports/scr04Catalog';
import { text } from '@/texts';
import { useUiSession } from '@/state/session';
import { ScreenState } from '@/pages/shared/ScreenState';
import { CatalogView } from './CatalogView';
import { Changes } from './Changes';
import { StandardForm } from './StandardForm';
import { Versions } from './Versions';
import { Refresh, Result, useCatalogQuery } from './shared';
import styles from './catalog.module.css';

function ZoneCatalog({
  zoneId,
  plant,
  overview = false,
  declare = false,
}: {
  readonly zoneId: string;
  readonly plant: Plant;
  readonly overview?: boolean;
  readonly declare?: boolean;
}) {
  const { principal } = useUiSession();
  const [form, setForm] = useState<'declare' | Standard | null>(declare ? 'declare' : null);
  const query = useCatalogQuery('GET /zones/{zone_id}/catalog', zoneId, async (signal) => {
    const version = await catalogPort.current.read({ path: { zone_id: zoneId } }, signal);
    return {
      version,
      catalog: catalogPayload(version, zoneId, principal?.organization.organization_id ?? ''),
    };
  });
  const regression = useCatalogQuery(
    'GET /zones/{zone_id}/regression',
    zoneId,
    (signal) => catalogPort.regression.read({ path: { zone_id: zoneId } }, signal),
    !overview,
  );
  const timeZone = plant.timezone;
  const data = query.data;
  return (
    <section>
      {overview ? (
        <Link to={`/zones/${zoneId}/catalog`}>
          {plant.zones.find((zone) => zone.zone_id === zoneId)?.name ??
            text('scr04.catalog.current')}
        </Link>
      ) : (
        <Link to={`/plants/${plant.plant_id}/catalog`}>{plant.name}</Link>
      )}
      <Refresh query={query} />
      <Result query={query}>
        {query.data === undefined ? null : (
          <CatalogView
            {...query.data}
            timeZone={timeZone}
            organizationId={principal?.organization.organization_id ?? ''}
          />
        )}
      </Result>
      {overview ? null : (
        <>
          <Refresh query={regression} />
          <Result query={regression}>
            {regression.data?.state === 'pending' ? (
              <GateStateChip gate={{ state: 'regression_pending' }} timeZone={timeZone} />
            ) : (
              <p>{text('scr04.catalog.regression_current')}</p>
            )}
          </Result>
          <Link to={`/zones/${zoneId}/commissioning`}>{text('scr04.catalog.regression')}</Link>
          <button
            type="button"
            onClick={() => {
              setForm('declare');
            }}
          >
            {text('scr04.catalog.declare')}
          </button>
          {data === undefined
            ? null
            : data.catalog.standards.map((standard) => (
                <div key={standard.standard_id}>
                  <button
                    type="button"
                    onClick={() => {
                      setForm(standard);
                    }}
                  >
                    {text('scr04.catalog.new_version')} · {standard.title_es}
                  </button>
                  <Changes
                    zoneId={zoneId}
                    version={data.version}
                    standardId={standard.standard_id}
                  />
                </div>
              ))}
          {form === null ? null : (
            <>
              <button
                type="button"
                onClick={() => {
                  setForm(null);
                }}
              >
                {text('scr04.catalog.cancel')}
              </button>
              <StandardForm
                key={form === 'declare' ? 'declare' : `${form.standard_id}:${String(form.version)}`}
                zoneId={zoneId}
                plantId={plant.plant_id}
                {...(form === 'declare' ? {} : { standard: form })}
              />
            </>
          )}
          {query.data === undefined ? null : (
            <Changes zoneId={zoneId} version={query.data.version} />
          )}
          {query.status === 'pending' ? null : (
            <Versions
              key={query.data?.version.catalog_version ?? 'initial'}
              zoneId={zoneId}
              timeZone={timeZone}
            />
          )}
        </>
      )}
    </section>
  );
}

export default function Page() {
  const { zone_id, plant_id } = useParams();
  const standards = useMatch('/zones/:zone_id/standards') !== null;
  const hierarchy = useCatalogQuery('GET /hierarchy', plant_id ?? zone_id ?? 'all', (signal) =>
    catalogPort.hierarchy.read({}, signal),
  );
  const plant = hierarchy.data?.plants.find((item) =>
    plant_id === undefined
      ? item.zones.some((zone) => zone.zone_id === zone_id)
      : item.plant_id === plant_id,
  );
  if (hierarchy.status === 'success' && plant === undefined)
    return <ScreenState state="not_available_in_scope" />;
  return (
    <main className={styles.catalog}>
      <h1>{text('scr04.catalog.title')}</h1>
      <Refresh query={hierarchy} />
      <Result
        query={hierarchy}
        empty={plant !== undefined && zone_id === undefined && plant.zones.length === 0}
      >
        {plant === undefined ? null : zone_id === undefined ? (
          plant.zones.map((zone) => (
            <ZoneCatalog key={zone.zone_id} zoneId={zone.zone_id} plant={plant} overview />
          ))
        ) : (
          <ZoneCatalog
            key={`${zone_id}:${String(standards)}`}
            zoneId={zone_id}
            plant={plant}
            declare={standards}
          />
        )}
      </Result>
    </main>
  );
}
