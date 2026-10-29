/**
 * RV-12, detalle de hallazgo (SCR-08; BR-APP-36, 40, 58 y 64). Selección explícita de campos:
 * ningún componente serializa el objeto recibido. Cada sección se degrada por separado: el texto
 * del estándar, el estado «ahora», el registro del contrato o un clip pueden faltar sin ocultar el
 * resto del hallazgo.
 */
import type { ReactNode } from 'react';
import platform from '../../../../../backend/resources/labels.platform.es.json';
import { ClipGallery } from '@/components/BlurredClipPlayer/ClipGallery';
import { ObservabilityBadge } from '@/components/ObservabilityBadge/ObservabilityBadge';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ReviewResolutions } from '@/pages/shared/ReviewResolutions';
import type { ReviewResolutionItem } from '@/ports/shared';
import {
  reasonList,
  type ClassificationItem,
  type CoverageNow,
  type DetailClip,
  type DetailRecord,
  type FindingDetail,
  type FindingRecord,
} from '@/ports/scr08Detail';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';
import { capitalizedTable, hasLabel, label } from '@/texts/labels';
import { formatDuration } from '@/texts/duration';

const roles: Readonly<Record<string, string>> = capitalizedTable(platform.role);

function roleLabel(role: string | null): string {
  return role !== null && Object.hasOwn(roles, role)
    ? (roles[role] ?? text('common.unknown'))
    : text('common.unknown');
}

/** Duración en segundos o minutos, nunca en milisegundos (VIG-366, S3-14). */
function duration(ms: number): string {
  return formatDuration(ms) ?? text('common.unknown');
}

export function reasonLabel(outcome: string, category: string): string {
  const list = reasonList(outcome);
  return list === null ? text('common.unknown') : label(list, category);
}

/** Estado de la sección «ahora»: aún sin pedir, pidiéndose, recibido o no disponible. */
export type NowState =
  | { readonly kind: 'hidden' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly coverage: CoverageNow }
  | { readonly kind: 'unavailable' };

/** Estado del registro del contrato de los tramos. */
export type RecordsState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly records: readonly FindingRecord[] }
  | { readonly kind: 'unavailable' };

function Field({ name, children }: { readonly name: string; readonly children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Instant({
  instant,
  timeZone,
  aggregated,
}: {
  readonly instant: string;
  readonly timeZone: string;
  readonly aggregated: boolean;
}) {
  return (
    <>
      <TimestampWithOffset instant={instant} timeZone={timeZone} />
      {aggregated && <> ({text('scr08.detail.aggregated')})</>}
    </>
  );
}

/** Los dos semáforos de BR-APP-36, cada uno con su etiqueta. */
function Observability({
  atEvent,
  now,
  timeZone,
}: {
  readonly atEvent: string;
  readonly now: NowState;
  readonly timeZone: string;
}) {
  return (
    <section aria-label={text('scr08.detail.observability')}>
      <h2>{text('scr08.detail.observability')}</h2>
      <div role="group" aria-label={text('scr08.detail.at_event')} data-part="at_event">
        <h3>{text('scr08.detail.at_event')}</h3>
        <ObservabilityBadge state={atEvent} />
      </div>
      {now.kind !== 'hidden' && (
        <div role="group" aria-label={text('scr08.detail.now')} data-part="now">
          <h3>{text('scr08.detail.now')}</h3>
          {now.kind === 'loading' && (
            <p role="status" aria-busy="true">
              {text('screen_state.loading')}
            </p>
          )}
          {now.kind === 'unavailable' && (
            <p role="status">{text('scr08.detail.now_unavailable')}</p>
          )}
          {now.kind === 'ready' && (
            <ObservabilityBadge
              state={now.coverage.state}
              {...(now.coverage.causes[0] === undefined ? {} : { cause: now.coverage.causes[0] })}
              since={{ instant: now.coverage.instant, timeZone }}
            />
          )}
        </div>
      )}
    </section>
  );
}

function Standard({
  detail,
  timeZone,
}: {
  readonly detail: FindingDetail;
  readonly timeZone: string;
}) {
  const { standard } = detail;
  const unavailable =
    standard.declared_text === null ||
    detail.notices.some((notice) => notice.code === 'standard_text_unavailable');
  return (
    <section aria-label={text('scr08.detail.standard')} data-part="standard">
      <h2>{text('scr08.detail.standard')}</h2>
      <dl>
        {standard.title_es !== null && (
          <Field name={text('scr08.detail.standard')}>{standard.title_es}</Field>
        )}
        <Field name={text('scr08.detail.standard_version')}>{standard.version}</Field>
        {standard.effective_from !== null && (
          <Field name={text('scr08.detail.standard_effective_from')}>
            <TimestampWithOffset instant={standard.effective_from} timeZone={timeZone} />
          </Field>
        )}
      </dl>
      {unavailable ? (
        <p role="status" data-part="standard_unavailable">
          {text('scr08.detail.standard_unavailable')}
        </p>
      ) : (
        <figure>
          <figcaption>{text('scr08.detail.standard_text')}</figcaption>
          <blockquote data-part="declared_text">{standard.declared_text}</blockquote>
        </figure>
      )}
      {standard.superseded_since !== null && (
        <p role="note" data-part="superseded">
          {text('scr08.detail.standard_superseded')}{' '}
          <TimestampWithOffset instant={standard.superseded_since} timeZone={timeZone} />
          {standard.superseded_in_catalog_version !== null && (
            <>
              {' · '}
              {text('scr08.detail.standard_superseded_catalog')}{' '}
              {standard.superseded_in_catalog_version}
            </>
          )}
        </p>
      )}
    </section>
  );
}

function Notices({
  detail,
  timeZone,
}: {
  readonly detail: FindingDetail;
  readonly timeZone: string;
}) {
  if (detail.notices.length === 0) return null;
  return (
    <section aria-label={text('scr08.detail.notices')}>
      <h2>{text('scr08.detail.notices')}</h2>
      <ul>
        {detail.notices.map((notice, index) => (
          <li key={`${notice.code}:${String(index)}`} data-notice={notice.code}>
            {notice.code === 'agreement_revoked' && notice.since !== null ? (
              <>
                {text('scr08.detail.agreement_revoked')}{' '}
                <TimestampWithOffset instant={notice.since} timeZone={timeZone} />
              </>
            ) : (
              label('finding_detail_notice', notice.code)
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Summary({
  detail,
  timeZone,
  zone,
}: {
  readonly detail: FindingDetail;
  readonly timeZone: string;
  readonly zone: string | null;
}) {
  const aggregated = detail.time_aggregated;
  return (
    <section aria-label={text('scr08.detail.summary')}>
      <h2>{text('scr08.detail.summary')}</h2>
      <dl>
        {zone !== null && <Field name={text('scr08.labels.zone')}>{zone}</Field>}
        <Field name={text('scr08.detail.family')}>{label('predicate_family', detail.family)}</Field>
        <Field name={text('scr08.detail.tier')}>{label('evidence_tier', detail.tier)}</Field>
        <Field name={text('scr08.detail.state')}>
          {label('finding_lifecycle_state', detail.lifecycle_state)}
        </Field>
        <Field name={text('scr08.detail.occurred_at')}>
          <Instant instant={detail.occurred_at} timeZone={timeZone} aggregated={aggregated} />
          {aggregated && detail.aggregation_window_minutes !== null && (
            <span data-part="window">
              {' · '}
              {detail.aggregation_window_minutes} {text('scr08.detail.aggregated_minutes')}
            </span>
          )}
        </Field>
        <Field name={text('scr08.detail.age_days')}>{detail.age_days}</Field>
        <Field name={text('scr08.detail.gate')}>
          {label('gate_status', detail.gate_at_event.status)} · {text('scr08.detail.gate_from')}{' '}
          <TimestampWithOffset instant={detail.gate_at_event.effective_from} timeZone={timeZone} />
        </Field>
        {detail.episode !== null && (
          <>
            <Field name={text('scr08.detail.episode_duration')}>
              {duration(detail.episode.total_episode_duration_ms)}
            </Field>
            <Field name={text('scr08.detail.condition_duration')}>
              {duration(detail.episode.total_condition_duration_ms)}
            </Field>
            <Field name={text('scr08.detail.segments')}>
              {detail.episode.segment_count} / {detail.episode.expected_segment_count}
            </Field>
            {detail.episode.missing_segment_indexes.length > 0 && (
              <Field name={text('scr08.detail.segments_missing')}>
                {detail.episode.missing_segment_indexes.join(', ')}
              </Field>
            )}
          </>
        )}
      </dl>
    </section>
  );
}

/** Señales del tramo, etiquetadas por rol y valor (SignalReading). */
function Signals({ record }: { readonly record: DetailRecord }) {
  if (record.signals.length === 0) return <p>{text('scr08.detail.no_signals')}</p>;
  return (
    <ul aria-label={text('scr08.detail.signals')}>
      {record.signals.map((signal, index) => (
        <li key={`${signal.role}:${String(index)}`} data-part="signal">
          {label('signal_role', signal.role)}: {label('signal_value', signal.value)}
          {!signal.valid && <> ({text('scr08.detail.signal_invalid')})</>}
        </li>
      ))}
    </ul>
  );
}

/**
 * La parte del tramo que solo trae el `Finding` del contrato, ya validado: hora del nodo con su
 * desfase, cámaras con la originaria marcada, duraciones y versiones. Si el `Finding` no cumple el
 * contrato, solo el error.
 */
export function ContractPart({
  record,
  receivedAt,
  timeZone,
  aggregated,
}: {
  readonly record: FindingRecord | undefined;
  readonly receivedAt: string;
  readonly timeZone: string;
  readonly aggregated: boolean;
}) {
  if (record === undefined) return null;
  if (!record.ok) {
    return (
      <p role="alert" data-part="contract_error" data-code={record.error.code}>
        {text('scr08.detail.contract_error')}
      </p>
    );
  }
  const { finding } = record;
  return (
    <dl data-part="contract">
      <Field name={text('scr08.detail.received_at')}>
        {aggregated ? (
          <Instant instant={receivedAt} timeZone={timeZone} aggregated />
        ) : (
          <TimestampWithOffset
            instant={receivedAt}
            timeZone={timeZone}
            nodeTime={finding.node_time.started_at}
          />
        )}
      </Field>
      <Field name={text('scr08.detail.observability')}>
        {label('observability_state', finding.observability_state)}
      </Field>
      <Field name={text('scr08.detail.condition_duration')}>{duration(finding.condition_duration_ms)}</Field>
      <Field name={text('scr08.detail.episode_duration')}>{duration(finding.episode_duration_ms)}</Field>
      <Field name={text('scr08.detail.cameras')}>
        <ul>
          {finding.cameras.map((camera, index) => (
            <li
              key={`${camera.camera_id}:${String(index)}`}
              data-part="camera"
              data-originating={camera.originating}
            >
              {text('scr08.detail.camera')} {index + 1}:{' '}
              {camera.originating
                ? text('scr08.detail.originating')
                : text('scr08.detail.redundant')}
            </li>
          ))}
        </ul>
      </Field>
      <Field name={text('scr08.detail.model_version')}>{finding.model_version}</Field>
      <Field name={text('scr08.detail.software_version')}>{finding.software_version}</Field>
    </dl>
  );
}

function Clips({
  clips,
  clock,
  cameraIndex,
}: {
  readonly clips: readonly DetailClip[];
  readonly clock: Clock;
  readonly cameraIndex: (cameraId: string) => number | null;
}) {
  if (clips.length === 0) return <p>{text('scr08.detail.no_clips')}</p>;
  return (
    <ClipGallery
      clock={clock}
      clips={clips.map((clip, index) => {
        const camera = cameraIndex(clip.camera_id);
        return {
          key: clip.clip_id,
          evidenceId: clip.evidence_id,
          label: `${text('scr08.detail.clip')} ${String(index + 1)} · ${label('clip_segment', clip.segment)}${
            camera === null ? '' : ` · ${text('scr08.detail.camera')} ${String(camera + 1)}`
          }`,
        };
      })}
    />
  );
}

function Records({
  detail,
  records,
  timeZone,
  clock,
  canViewClips,
}: {
  readonly detail: FindingDetail;
  readonly records: RecordsState;
  readonly timeZone: string;
  readonly clock: Clock | null;
  readonly canViewClips: boolean;
}) {
  if (detail.records.length === 0) return null;
  return (
    <section aria-label={text('scr08.detail.records')}>
      <h2>{text('scr08.detail.records')}</h2>
      {records.kind === 'loading' && <p role="status">{text('scr08.detail.contract_loading')}</p>}
      {records.kind === 'unavailable' && (
        <p role="status">{text('scr08.detail.contract_unavailable')}</p>
      )}
      <ol>
        {detail.records.map((record) => {
          const contract =
            records.kind === 'ready'
              ? records.records.find((item) => item.record_id === record.record_id)
              : undefined;
          const cameras = contract?.ok === true ? contract.finding.cameras : [];
          const cameraIndex = (cameraId: string) => {
            const index = cameras.findIndex((camera) => camera.camera_id === cameraId);
            return index < 0 ? null : index;
          };
          return (
            <li key={record.record_id} data-part="record">
              {/* «Tramo 2 · Hallazgo»; sin número ni tipo conocidos, solo «Tramo» (S3-17). */}
              <h3>
                {[
                  record.segment_index === null
                    ? text('scr08.detail.record')
                    : `${text('scr08.detail.record')} ${String(record.segment_index + 1)}`,
                  ...(hasLabel('record_kind', record.record_type)
                    ? [label('record_kind', record.record_type)]
                    : []),
                ].join(' · ')}
              </h3>
              {contract === undefined && (
                <p>
                  {text('scr08.detail.received_at')}:{' '}
                  <Instant
                    instant={record.received_at}
                    timeZone={timeZone}
                    aggregated={detail.time_aggregated}
                  />
                </p>
              )}
              <ContractPart
                record={contract}
                receivedAt={record.received_at}
                timeZone={timeZone}
                aggregated={detail.time_aggregated}
              />
              <h4>{text('scr08.detail.signals')}</h4>
              <Signals record={record} />
              {canViewClips && clock !== null && (
                <>
                  <h4>{text('scr08.detail.clips')}</h4>
                  <Clips clips={record.clips} clock={clock} cameraIndex={cameraIndex} />
                </>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/**
 * Historial completo de clasificaciones (SUP-U05-04): ninguna se edita ni se borra (P4). Si el
 * hallazgo pasó por la cola, su resolución va antes, distinguida de las clasificaciones (VIG-359).
 */
export function History({
  classifications,
  reviewResolutions,
  timeAggregated,
  timeZone,
}: {
  readonly classifications: readonly ClassificationItem[];
  readonly reviewResolutions: readonly ReviewResolutionItem[];
  readonly timeAggregated: boolean;
  readonly timeZone: string;
}) {
  return (
    <section aria-label={text('scr08.detail.history')}>
      <h2>{text('scr08.detail.history')}</h2>
      <ReviewResolutions
        resolutions={reviewResolutions}
        timeAggregated={timeAggregated}
        timeZone={timeZone}
      />
      {classifications.length === 0 ? (
        <p>{text('scr08.detail.history_empty')}</p>
      ) : (
        <ol>
          {classifications.map((item) => (
            <li key={item.classification_id} data-part="classification">
              <p>
                <strong>
                  {text('scr08.detail.sequence')} {item.sequence_in_subject}:{' '}
                  {label('classification_outcome', item.outcome)}
                </strong>
              </p>
              <dl>
                <Field name={text('scr08.detail.reason')}>
                  {reasonLabel(item.outcome, item.reason_category)}
                  {' · '}
                  <span data-part="reason_text">{item.reason_text}</span>
                </Field>
                {item.previous_outcome !== null && (
                  <Field name={text('scr08.detail.previous')}>
                    {label('classification_outcome', item.previous_outcome)}
                  </Field>
                )}
                {item.reinforced_reason && (
                  <Field name={text('scr08.detail.reinforced')}>{text('scr08.detail.yes')}</Field>
                )}
                <Field name={text('scr08.detail.classified_at')}>
                  <Instant
                    instant={item.classified_at}
                    timeZone={timeZone}
                    aggregated={timeAggregated}
                  />
                </Field>
                <Field name={text('scr08.detail.classified_by')}>
                  {item.classified_by.display_name}
                </Field>
                <Field name={text('scr08.detail.role')}>
                  {roleLabel(item.classified_by.role_in_use)}
                </Field>
              </dl>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** El detalle completo, sin lecturas: lo usan la página y la vista RV-12. */
export function DetailView({
  detail,
  zone,
  timeZone,
  now,
  records,
  clock,
  canViewClips,
}: {
  readonly detail: FindingDetail;
  readonly zone: string | null;
  readonly timeZone: string;
  readonly now: NowState;
  readonly records: RecordsState;
  readonly clock: Clock | null;
  readonly canViewClips: boolean;
}) {
  return (
    <>
      <Notices detail={detail} timeZone={timeZone} />
      <Summary detail={detail} timeZone={timeZone} zone={zone} />
      <Observability atEvent={detail.observability_state_at_event} now={now} timeZone={timeZone} />
      <Standard detail={detail} timeZone={timeZone} />
      <Records
        detail={detail}
        records={records}
        timeZone={timeZone}
        clock={clock}
        canViewClips={canViewClips}
      />
    </>
  );
}
