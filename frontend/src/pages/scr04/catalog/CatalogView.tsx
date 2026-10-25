/** RV-07: proyección explícita; nunca se recorren campos arbitrarios del servidor. */
import type { Catalog, CatalogVersion } from '@/ports/scr04Catalog';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { ScreenState } from '@/pages/shared/ScreenState';

export function CatalogView({
  catalog,
  version,
  timeZone,
  organizationId,
  concessionOrganizationId = null,
  reportContractError,
}: {
  readonly catalog: Catalog;
  readonly version: Pick<CatalogVersion, 'single_occupancy' | 'aggregation_window_minutes'>;
  readonly timeZone: string;
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
}) {
  if (
    catalog.organization_id !== organizationId &&
    catalog.organization_id !== concessionOrganizationId
  ) {
    reportContractError?.({ code: 'foreign_organization' });
    return <ScreenState state="error" />;
  }
  return (
    <section aria-label={text('scr04.catalog.current')}>
      <h2>
        {catalog.zone_code} · {text('scr04.catalog.version')} {catalog.version}
      </h2>
      <dl>
        <dt>{text('scr04.catalog.single_occupancy')}</dt>
        <dd>{text(version.single_occupancy ? 'scr04.catalog.yes' : 'scr04.catalog.no')}</dd>
        <dt>{text('scr04.catalog.window')}</dt>
        <dd>{version.aggregation_window_minutes}</dd>
        <dt>{text('scr04.catalog.review')}</dt>
        <dd>{catalog.thresholds.review}</dd>
        <dt>{text('scr04.catalog.publication')}</dt>
        <dd>{catalog.thresholds.publication}</dd>
        <dt>{text('scr04.catalog.coverage')}</dt>
        <dd>
          {catalog.minimum_coverage.required_count} ·{' '}
          {catalog.minimum_coverage.required_camera_ids.join(', ')}
        </dd>
        <dt>{text('scr04.catalog.clip_window')}</dt>
        <dd>
          {catalog.clip_window.pre_seconds} / {catalog.clip_window.post_seconds}
        </dd>
      </dl>
      {catalog.standards.map((standard) => (
        <article key={standard.standard_id} aria-label={standard.title_es}>
          <h3>{standard.title_es}</h3>
          <p>{standard.declared_text}</p>
          <dl>
            <dt>{text('scr04.catalog.family')}</dt>
            <dd>{label('predicate_family', standard.family)}</dd>
            <dt>{text('scr04.catalog.tier')}</dt>
            <dd>{text('scr04.catalog.tier_policy_signal')}</dd>
            <dt>{text('scr04.catalog.version')}</dt>
            <dd>{standard.version}</dd>
            <dt>{text('scr04.catalog.duration')}</dt>
            <dd>{standard.predicate.min_duration_ms}</dd>
            <dt>{text('scr04.catalog.effective_from')}</dt>
            <dd>
              <TimestampWithOffset instant={standard.effective_from} timeZone={timeZone} />
            </dd>
            <dt>{text('scr04.catalog.author')}</dt>
            <dd>{standard.declared_by.display_name}</dd>
          </dl>
        </article>
      ))}
    </section>
  );
}
