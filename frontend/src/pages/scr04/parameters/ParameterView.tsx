/** Proyección explícita RV-07; nunca serializa el catálogo recibido ni la conexión de cámara. */
import type { Catalog } from '@/ports/scr04Parameters';
import { text } from '@/texts';

export function ParameterView({ data }: { readonly data: Catalog }) {
  return (
    <dl>
      <dt>{text('scr04.parameters.version')}</dt>
      <dd>{data.catalog_version}</dd>
      <dt>{text('scr04.parameters.issued_at')}</dt>
      <dd>{data.issued_at}</dd>
      <dt>{text('scr04.parameters.reason')}</dt>
      <dd>{data.reason_es}</dd>
    </dl>
  );
}
