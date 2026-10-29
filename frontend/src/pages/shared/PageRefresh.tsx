/**
 * Una sola actualización por página (sistema visual v1, S3-03 y S3-08 de VIG-365): «Actualizado
 * hace N s · Actualizar» va una vez, en la cabecera de la página, y no en cada sección.
 *
 * - «Actualizado» es la respuesta más antigua de las lecturas que la página muestra: nunca dice
 *   más fresco de lo que está la parte más vieja.
 * - «Actualizar» vuelve a pedir todas las lecturas activas de la pantalla (`useScreenRefresh`),
 *   también las de sus secciones; las auditadas que la persona aún no abrió no se piden.
 * - La marca es «a demanda» si alguna lectura de la página es auditada (BR-APP-54): lo auditado
 *   solo se renueva al pulsar, aunque lo demás se renueve solo cada 60 s. Sin auditadas, es «cada
 *   60 s» si alguna lectura se renueva sola: la jerarquía o una referencia, que no se renuevan
 *   solas, no vuelven «a demanda» una bandeja que sí lo hace.
 */
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { currentTransport } from '@/ports/base/http';
import { policyFor, useScreenRefresh, type ReadOperation, type Screen } from '@/state/query';

export interface PageRead {
  readonly fetchedAt: number | null;
  readonly operation: ReadOperation;
}

/** La respuesta más antigua de las lecturas ya recibidas; `null` si ninguna ha llegado. */
function oldestFetch(reads: readonly PageRead[]): number | null {
  const times = reads.flatMap((read) => (read.fetchedAt === null ? [] : [read.fetchedAt]));
  return times.length === 0 ? null : Math.min(...times);
}

export function PageRefresh({
  screen,
  reads,
}: {
  readonly screen: Screen;
  /** Las lecturas que la página muestra, para «Actualizado hace» y la marca de la consulta. */
  readonly reads: readonly PageRead[];
}) {
  const { isFetching, refresh } = useScreenRefresh(screen);
  const policies = reads.map((read) => policyFor(read.operation));
  const onDemand =
    policies.some((policy) => policy.explicitOnly) || !policies.some((policy) => policy.periodic);
  return (
    <RefreshIndicator
      clock={currentTransport().clock}
      fetchedAt={oldestFetch(reads)}
      isFetching={isFetching}
      refreshPolicy={onDemand ? 'on_demand' : 'periodic_60s'}
      onRefresh={() => {
        void refresh();
      }}
    />
  );
}
