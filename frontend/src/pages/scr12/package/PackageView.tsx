/** RV-16: proyección explícita de los campos disponibles; ningún objeto abierto se expande. */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { TechnicalDetails } from '@/pages/shared/TechnicalDetails';
import type { MonthlyPackage } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import { label } from '@/texts/labels';

const COUNTS = [
  'received',
  'classified',
  'confirmed',
  'discarded',
  'closed',
  'overdue',
  'unclosable',
  'reopened',
] as const;

export function PackageView({
  data,
  timeZone,
}: {
  readonly data: MonthlyPackage;
  readonly timeZone: string;
}) {
  return (
    <>
      <p>
        {text('scr12.period')}: {data.period}
      </p>
      <p>
        {text('scr12.state')}: {label('monthly_package_state', data.state)}
      </p>
      <p>
        {text('scr12.generated')}:{' '}
        <TimestampWithOffset instant={data.generated_at} timeZone={timeZone} />
      </p>
      <section aria-label={text('scr12.summary')}>
        <h2>{text('scr12.summary')}</h2>
        <dl>
          {COUNTS.map((key) => (
            <div key={key}>
              <dt>{text(`scr12.${key}`)}</dt>
              <dd>{data.findings_summary[key]}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section aria-label={text('scr12.recurrence')}>
        <h2>{text('scr12.recurrence')}</h2>
        {data.recurrence_summary.length === 0 ? (
          <p>{text('scr12.recurrence_empty')}</p>
        ) : (
          <ul>
            {data.recurrence_summary.map((entry, index) => (
              <li key={index}>
                {label('predicate_family', entry.family)}: {text('scr12.recurrence_count')}{' '}
                {entry.episodes_after_closure}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label={text('scr12.previous')}>
        <h2>{text('scr12.previous')}</h2>
        {data.commitments_from_previous_period.length === 0 ? (
          <p>{text('scr12.previous_empty')}</p>
        ) : (
          <ul>
            {data.commitments_from_previous_period.map((entry) => (
              <li key={entry.commitment_id}>
                {/* VIG-366: el estado a la vista; el identificador, plegado. */}
                {label('commitment_state', entry.state)}
                <TechnicalDetails
                  items={[{ name: text('common.identifier'), value: entry.commitment_id }]}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label={text('scr12.exposure')}>
        <h2>{text('scr12.exposure')}</h2>
        <p>{text('scr12.exposure_unavailable')}</p>
      </section>
      <section aria-label={text('scr12.checkpoint')}>
        <h2>{text('scr12.checkpoint')}</h2>
        <p>{text('scr12.checkpoint_unavailable')}</p>
        {data.record_id !== null && (
          <TechnicalDetails items={[{ name: text('scr12.record'), value: data.record_id }]} />
        )}
      </section>
      <p>{text('scr12.sample_metadata_unavailable')}</p>
    </>
  );
}
