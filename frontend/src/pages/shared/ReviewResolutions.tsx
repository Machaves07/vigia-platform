/**
 * La resolución de la cola en el historial del hallazgo (VIG-359; T05 y T06): la decisión humana
 * que confirmó o descartó la detección, con su motivo, quién la tomó, su rol en uso y la fecha. No
 * es una clasificación (no tiene secuencia ni efecto sobre el lazo), así que va en su propia lista,
 * antes de las clasificaciones. Selección explícita de campos: nunca serializa el objeto recibido.
 */
import platform from '../../../../backend/resources/labels.platform.es.json';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { reasonCategories, type ReviewResolutionItem } from '@/ports/scr10ReviewQueue';
import { text } from '@/texts';
import { label } from '@/texts/labels';

const roles: Readonly<Record<string, string>> = platform.role;

function roleLabel(role: string): string {
  return Object.hasOwn(roles, role)
    ? (roles[role] ?? text('common.unknown'))
    : text('common.unknown');
}

function reasonLabel(outcome: string, category: string): string {
  const found = reasonCategories(outcome).find(({ value }) => value === category);
  return found === undefined ? text('common.unknown') : label(found.list, category);
}

export function ReviewResolutions({
  resolutions,
  timeAggregated,
  timeZone,
  headingLevel = 3,
}: {
  readonly resolutions: readonly ReviewResolutionItem[];
  readonly timeAggregated: boolean;
  readonly timeZone: string;
  readonly headingLevel?: 2 | 3;
}) {
  if (resolutions.length === 0) return null;
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <section aria-label={text('finding_journey.resolution')} data-part="review_resolutions">
      <Heading>{text('finding_journey.resolution')}</Heading>
      <ol>
        {resolutions.map((item) => (
          <li key={item.resolution_id} data-part="review_resolution" data-outcome={item.outcome}>
            <p>
              <strong>
                {text('finding_journey.resolution_outcome')}:{' '}
                {label('review_resolution_outcome', item.outcome)}
              </strong>
            </p>
            <p>
              {text('finding_journey.resolution_reason')}:{' '}
              {reasonLabel(item.outcome, item.reason_category)}
            </p>
            <p data-part="reason_text">{item.reason_text}</p>
            <p>
              {text('finding_journey.resolved_at')}:{' '}
              <TimestampWithOffset instant={item.resolved_at} timeZone={timeZone} />
              {timeAggregated && <> ({text('finding_journey.aggregated')})</>}
            </p>
            <p data-role-in-use={item.resolved_by.role_in_use}>
              {text('finding_journey.resolved_by')}: {item.resolved_by.display_name}{' '}
              {text('finding_journey.role')} {roleLabel(item.resolved_by.role_in_use)}
            </p>
          </li>
        ))}
      </ol>
    </section>
  );
}
