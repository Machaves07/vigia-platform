/**
 * RV-13, historial de clasificaciones (SUP-U05-04): todas, en el orden del servidor, cada una como
 * un registro propio; ninguna se edita ni se borra (BR-APP-24, H-22). Selección explícita de
 * campos: nunca serializa el objeto recibido. La resolución de la cola, si la hay, va antes y
 * aparte: no es una clasificación (VIG-359).
 */
import platform from '../../../../../backend/resources/labels.platform.es.json';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ReviewResolutions } from '@/pages/shared/ReviewResolutions';
import { reasonCategories, type ClassificationItem } from '@/ports/scr09Classify';
import type { ReviewResolutionItem } from '@/ports/shared';
import { text } from '@/texts';
import { capitalizedTable, label } from '@/texts/labels';

const roles: Readonly<Record<string, string>> = capitalizedTable(platform.role);

function roleLabel(role: string): string {
  return Object.hasOwn(roles, role)
    ? (roles[role] ?? text('common.unknown'))
    : text('common.unknown');
}

function reasonLabel(outcome: string, category: string): string {
  const { list } = reasonCategories(outcome);
  return list === '' ? text('common.unknown') : label(list, category);
}

const NO_RESOLUTIONS: readonly ReviewResolutionItem[] = [];

export function ClassificationHistory({
  classifications,
  reviewResolutions = NO_RESOLUTIONS,
  timeAggregated,
  timeZone,
}: {
  readonly classifications: readonly ClassificationItem[];
  /** La resolución de la cola, si el hallazgo pasó por ella: va antes y aparte (VIG-359). */
  readonly reviewResolutions?: readonly ReviewResolutionItem[];
  readonly timeAggregated: boolean;
  readonly timeZone: string;
}) {
  return (
    <>
      <ReviewResolutions
        resolutions={reviewResolutions}
        timeAggregated={timeAggregated}
        timeZone={timeZone}
      />
      <Classifications
        classifications={classifications}
        timeAggregated={timeAggregated}
        timeZone={timeZone}
      />
    </>
  );
}

function Classifications({
  classifications,
  timeAggregated,
  timeZone,
}: {
  readonly classifications: readonly ClassificationItem[];
  readonly timeAggregated: boolean;
  readonly timeZone: string;
}) {
  if (classifications.length === 0) return <p>{text('scr09.classify.history_empty')}</p>;
  return (
    <ol aria-label={text('scr09.classify.history')}>
      {classifications.map((item) => (
        <li key={item.classification_id} data-part="classification">
          <p>
            <strong>
              {text('scr09.classify.sequence')} {item.sequence_in_subject}:{' '}
              {label('classification_outcome', item.outcome)}
            </strong>
            {' · '}
            {reasonLabel(item.outcome, item.reason_category)}
          </p>
          <p data-part="reason_text">{item.reason_text}</p>
          {item.previous_outcome !== null && (
            <p>
              {text('scr09.classify.previous')}:{' '}
              {label('classification_outcome', item.previous_outcome)}
            </p>
          )}
          {item.reinforced_reason && <p>{text('scr09.classify.reinforced_yes')}</p>}
          <p>
            {text('scr09.classify.effect')}: {label('effect_on_loop', item.effect_on_loop)}
          </p>
          <p>
            {text('scr09.classify.classified_at')}:{' '}
            <TimestampWithOffset instant={item.classified_at} timeZone={timeZone} />
            {timeAggregated && <> ({text('scr09.classify.aggregated')})</>}
          </p>
          <p>
            {text('scr09.classify.classified_by')}: {item.classified_by.display_name}{' '}
            {text('scr09.classify.role')} {roleLabel(item.classified_by.role_in_use)}
          </p>
        </li>
      ))}
    </ol>
  );
}
