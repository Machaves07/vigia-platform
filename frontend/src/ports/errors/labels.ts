/** Etiquetas de las unidades dueñas, importadas en construcción; nunca una copia local. */
import platform from '../../../../backend/resources/labels.platform.es.json';
import loop from '../../../../backend/resources/labels.loop.es.json';

const detailLabels: Readonly<Record<string, string>> = Object.freeze({
  ...platform.catalog_detail_code,
  ...platform.fleet_detail_code,
  ...loop.loop_detail_code,
  ...loop.accreditation_detail_code,
  ...loop.notifications_detail_code,
});

export function detailLabel(code: string): string | undefined {
  return Object.hasOwn(detailLabels, code) ? detailLabels[code] : undefined;
}

export function exportStateLabel(state: string): string | undefined {
  const labels: Readonly<Record<string, string>> = loop.export_state;
  return Object.hasOwn(labels, state) ? labels[state] : undefined;
}
