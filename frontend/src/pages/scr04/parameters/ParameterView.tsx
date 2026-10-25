/** Proyección explícita RV-07; nunca serializa el catálogo recibido ni la conexión de cámara. */
import type { Catalog } from '@/ports/scr04Parameters';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { text } from '@/texts';

// Sin zona de planta en esta respuesta, se muestra explícitamente la del navegador (SCR-01).
export function ParameterView({
  data,
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone,
}: {
  readonly data: Catalog;
  readonly timeZone?: string;
}) {
  return (
    <dl>
      <dt>{text('scr04.parameters.version')}</dt>
      <dd>{data.catalog_version}</dd>
      <dt>{text('scr04.parameters.issued_at')}</dt>
      <dd>
        <TimestampWithOffset instant={data.issued_at} timeZone={timeZone} />
      </dd>
      <dt>{text('scr04.parameters.reason')}</dt>
      <dd>{data.reason_es}</dd>
    </dl>
  );
}
