/** Parámetros de zona: el servidor publica versiones y decide la regresión. */
import { Link, useParams } from 'react-router';
import { parametersPort } from '@/ports/scr04Parameters';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { ParameterForm } from './ParameterForm';
import { ParameterView } from './ParameterView';
import { Refresh, Result, useParametersScope } from './shared';
import styles from './parameters.module.css';

export default function Page() {
  const { zone_id = '' } = useParams();
  const context = useParametersScope();
  const catalogOperation = 'GET /zones/{zone_id}/catalog';
  const catalog = useScreenQuery({
    key: screenKey({
      screen: 'scr04',
      operation: catalogOperation,
      ...context,
      params: { zone_id },
    }),
    policy: policyFor(catalogOperation),
    read: ({ signal }) => parametersPort.catalog.read({ path: { zone_id } }, signal),
  });
  const operation = 'GET /zones/{zone_id}/regression';
  const regression = useScreenQuery({
    key: screenKey({ screen: 'scr04', operation, ...context, params: { zone_id } }),
    policy: policyFor(operation),
    read: ({ signal }) => parametersPort.regression.read({ path: { zone_id } }, signal),
  });
  return (
    <main className={styles.page} key={zone_id}>
      <h1>{text('scr04.parameters.title')}</h1>
      <Link to={`/zones/${zone_id}/catalog`}>{text('scr04.parameters.catalog')}</Link>
      <p>{text('scr04.parameters.signed')}</p>
      <Refresh query={catalog} />
      <Result query={catalog}>
        {catalog.data !== undefined && (
          <>
            <ParameterView data={catalog.data} />
            <ParameterForm zoneId={zone_id} initial={catalog.data} />
          </>
        )}
      </Result>
      <Refresh query={regression} />
      <Result query={regression}>
        {regression.data !== undefined && (
          <p>
            {text('scr04.parameters.regression')}:{' '}
            {text(`scr04.parameters.regression_${regression.data.state}`)}
          </p>
        )}
      </Result>
      <Link to={`/zones/${zone_id}/commissioning`}>{text('scr04.parameters.commissioning')}</Link>
    </main>
  );
}
