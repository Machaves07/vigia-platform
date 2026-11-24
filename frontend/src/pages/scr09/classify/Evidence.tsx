/**
 * Lo que se clasifica (VIG-363, hallazgo 29 de VIG-338): el resumen del hallazgo y el acceso a
 * sus clips, como en el detalle (SCR-08). Sale del mismo detalle auditado que la pantalla ya pide
 * para el aviso de estado: no hay otra lectura. Cada clip es un `BlurredClipPlayer`, que pide su
 * URL solo al pulsar reproducir (BR-APP-58); sin `evidence.read`, el resumen sin clips.
 * Selección explícita de campos (BR-APP-40): nada del objeto recibido se serializa.
 *
 * Junto al resumen, las señales de cada tramo durante el episodio (TASK-587, O-2 paso a): se
 * clasifica con el estado de la máquina a la vista, desde el mismo detalle, sin lectura nueva.
 *
 * Arriba, la foto del hallazgo con el aspecto de SCR-06 (TASK-590, A-128): la foto `0` del registro
 * ancla (el primer tramo), pedida **al montar**, una vez por apertura: al clasificar, la foto es lo
 * que se decide. Las demás fotos del tramo (la ráfaga, fase 2) solo con el gesto.
 *
 * Tras las señales, los valores de proceso de cada tramo (TASK-596, A-129): velocidad, metros y
 * estados de la máquina con su hora, del mismo detalle; sin bloque en ningún tramo, nada.
 */
import type { ReactNode } from 'react';
import { ClipGallery } from '@/components/BlurredClipPlayer/ClipGallery';
import { FindingPhoto } from '@/components/EpisodePhotoPanel/FindingPhoto';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { hasProcessValues, ProcessValues } from '@/pages/shared/ProcessValues';
import { SignalReadings } from '@/pages/shared/SignalReadings';
import { classifyClock, type FindingDetail } from '@/ports/scr09Classify';
import { text } from '@/texts';
import { label } from '@/texts/labels';

function Field({ name, children }: { readonly name: string; readonly children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  );
}

type FindingRecord = FindingDetail['records'][number];

/** Orden de tramo; un registro sin tramo va después, en el orden en que llega (orden estable). */
function bySegment(a: FindingRecord, b: FindingRecord): number {
  return (
    (a.segment_index ?? Number.POSITIVE_INFINITY) - (b.segment_index ?? Number.POSITIVE_INFINITY)
  );
}

function recordName(record: FindingRecord): string {
  const name = text('scr08.detail.record');
  return record.segment_index === null ? name : `${name} ${String(record.segment_index + 1)}`;
}

/** «Señales durante el episodio»: las de cada tramo de `detail.records`, en orden de tramo. */
function EpisodeSignals({
  records,
  timeZone,
  aggregated,
}: {
  readonly records: readonly FindingRecord[];
  readonly timeZone: string;
  readonly aggregated: boolean;
}) {
  const ordered = [...records].sort(bySegment);
  return (
    <section aria-label={text('scr09.classify.signals')} data-part="episode_signals">
      <h2>{text('scr09.classify.signals')}</h2>
      {ordered.length === 0 ? (
        <p data-part="no_records">{text('scr09.classify.signals_none')}</p>
      ) : (
        ordered.map((record) => (
          <div key={record.record_id} data-part="signal_record">
            <h3>{recordName(record)}</h3>
            <SignalReadings
              signals={record.signals}
              name={recordName(record)}
              timeZone={timeZone}
              aggregated={aggregated}
            />
          </div>
        ))
      )}
    </section>
  );
}

/** «Valores del proceso durante el episodio»: los tramos con bloque, en orden de tramo. */
function EpisodeProcessValues({
  records,
  timeZone,
  aggregated,
}: {
  readonly records: readonly FindingRecord[];
  readonly timeZone: string;
  readonly aggregated: boolean;
}) {
  const ordered = [...records].sort(bySegment).filter((r) => hasProcessValues(r.process_context));
  if (ordered.length === 0) return null;
  const instant = (value: string) => <TimestampWithOffset instant={value} timeZone={timeZone} />;
  return (
    <section aria-label={text('process_values.title')} data-part="episode_process_values">
      <h2>{text('process_values.title')}</h2>
      {ordered.map((record) => (
        <div key={record.record_id} data-part="process_record">
          {ordered.length > 1 && <h3>{recordName(record)}</h3>}
          <ProcessValues
            context={record.process_context}
            name={recordName(record)}
            instant={instant}
            aggregated={aggregated}
          />
        </div>
      ))}
    </section>
  );
}

export function FindingEvidence({
  detail,
  zone,
  timeZone,
  canViewClips,
}: {
  readonly detail: FindingDetail;
  readonly zone: string | null;
  readonly timeZone: string;
  readonly canViewClips: boolean;
}) {
  const clips = detail.records.flatMap((record) =>
    record.clips.map((clip) => ({ clip, segment: record.segment_index })),
  );
  const standard = detail.standard;
  const anchor = [...detail.records].sort(bySegment)[0];
  return (
    <>
      <FindingPhoto
        findingStateId={detail.finding_state_id}
        photos={anchor?.photos ?? []}
        segment={
          anchor === undefined || anchor.segment_index === null ? null : anchor.segment_index + 1
        }
        canView={canViewClips}
        request="on_mount"
        timeZone={timeZone}
        aggregated={detail.time_aggregated}
        clock={classifyClock()}
        headingLevel={2}
      />
      <section aria-label={text('scr08.detail.summary')} data-part="finding_evidence">
        <h2>{text('scr08.detail.summary')}</h2>
        <dl>
          {zone !== null && <Field name={text('scr08.labels.zone')}>{zone}</Field>}
          <Field name={text('scr08.detail.family')}>
            {label('predicate_family', detail.family)}
          </Field>
          <Field name={text('scr08.detail.tier')}>{label('evidence_tier', detail.tier)}</Field>
          <Field name={text('scr08.detail.state')}>
            {label('finding_lifecycle_state', detail.lifecycle_state)}
          </Field>
          <Field name={text('scr08.detail.occurred_at')}>
            <TimestampWithOffset instant={detail.occurred_at} timeZone={timeZone} />
            {detail.time_aggregated && <> ({text('scr08.detail.aggregated')})</>}
          </Field>
          <Field name={text('scr08.detail.standard')}>
            {standard.title_es ?? text('scr09.loop.unknown')} · v{standard.version}
          </Field>
        </dl>
        {canViewClips && (
          <>
            <h3>{text('scr08.detail.clips')}</h3>
            {clips.length === 0 ? (
              <p>{text('scr08.detail.no_clips')}</p>
            ) : (
              <ClipGallery
                clock={classifyClock()}
                clips={clips.map(({ clip, segment }, index) => ({
                  key: clip.clip_id,
                  evidenceId: clip.evidence_id,
                  label: `${text('scr08.detail.clip')} ${String(index + 1)} · ${label('clip_segment', clip.segment)}${
                    segment === null
                      ? ''
                      : ` · ${text('scr08.detail.record')} ${String(segment + 1)}`
                  }`,
                }))}
              />
            )}
          </>
        )}
      </section>
      <EpisodeSignals
        records={detail.records}
        timeZone={timeZone}
        aggregated={detail.time_aggregated}
      />
      <EpisodeProcessValues
        records={detail.records}
        timeZone={timeZone}
        aggregated={detail.time_aggregated}
      />
    </>
  );
}
