/** RV-07: comparación por identidad estable, con rótulos humanos y proyección cerrada. */
import {
  catalogOrganizationAllowed,
  type Catalog,
  type CatalogVersion,
} from '@/ports/scr04Catalog';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';
import { label } from '@/texts/labels';

type ComparisonVersion = Pick<CatalogVersion, 'single_occupancy' | 'aggregation_window_minutes'> & {
  readonly catalog: Catalog;
};
function fields(version: ComparisonVersion) {
  const catalog = version.catalog;
  const values = new Map<string, { field: string; value: string }>();
  const put = (key: string, value: string) => values.set(key, { field: text(key), value });
  put(
    'scr04.catalog.single_occupancy',
    text(version.single_occupancy ? 'scr04.catalog.yes' : 'scr04.catalog.no'),
  );
  put('scr04.catalog.window', String(version.aggregation_window_minutes));
  put('scr04.catalog.review', String(catalog.thresholds.review));
  put('scr04.catalog.publication', String(catalog.thresholds.publication));
  put(
    'scr04.catalog.coverage',
    JSON.stringify({
      required_count: catalog.minimum_coverage.required_count,
      required_camera_ids: catalog.minimum_coverage.required_camera_ids,
    }),
  );
  put(
    'scr04.catalog.clip_window',
    `${String(catalog.clip_window.pre_seconds)} / ${String(catalog.clip_window.post_seconds)}`,
  );
  for (const standard of catalog.standards) {
    const standardFields: Record<string, string> = {
      title_es: standard.title_es,
      declared_text: standard.declared_text,
      family: label('predicate_family', standard.family),
      tier: text('scr04.catalog.tier_policy_signal'),
      version: String(standard.version),
      author: standard.declared_by.display_name,
      effective_from: standard.effective_from,
      predicate: JSON.stringify(
        standard.predicate.all_of
          .map((condition) =>
            'presence' in condition
              ? text('scr04.catalog.presence')
              : `${label('signal_role', condition.signal_role)}: ${label('signal_value', condition.value)}`,
          )
          .sort(),
      ),
      duration: String(standard.predicate.min_duration_ms),
    };
    for (const [key, value] of Object.entries(standardFields)) {
      values.set(`${standard.standard_id}:${key}`, {
        field: `${text('scr04.catalog.standard')} · ${standard.title_es} · ${text(`scr04.catalog.${key}`)}`,
        value,
      });
    }
  }
  return values;
}

export function ComparisonView({
  before,
  after,
  organizationId,
  concessionOrganizationId = null,
  reportContractError,
}: {
  readonly before: ComparisonVersion;
  readonly after: ComparisonVersion;
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
}) {
  if (
    [before, after].some(
      (version) =>
        !catalogOrganizationAllowed(
          version.catalog.organization_id,
          organizationId,
          concessionOrganizationId,
        ),
    )
  ) {
    reportContractError?.({ code: 'foreign_organization' });
    return <ScreenState state="error" />;
  }
  const left = fields(before);
  const right = fields(after);
  const changes = [...new Set([...left.keys(), ...right.keys()])]
    .filter((key) => left.get(key)?.value !== right.get(key)?.value)
    .map((key) => ({
      key,
      field: (right.get(key) ?? left.get(key))?.field,
      before: left.get(key)?.value ?? '—',
      after: right.get(key)?.value ?? '—',
    }));
  return (
    <>
      <h3>{text('scr04.catalog.changed')}</h3>
      {changes.length === 0 ? <p>{text('scr04.catalog.no_changes')}</p> : null}
      <table>
        <caption>{text('scr04.catalog.compare')}</caption>
        <thead>
          <tr>
            <th scope="col">{text('scr04.catalog.changed')}</th>
            <th scope="col">{text('scr04.catalog.before')}</th>
            <th scope="col">{text('scr04.catalog.after')}</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => (
            <tr key={change.key}>
              <th scope="row">{change.field}</th>
              <td>{change.before}</td>
              <td>{change.after}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
