/**
 * Lo que se clasifica (VIG-363, hallazgo 29 de VIG-338): el resumen del hallazgo y el acceso a
 * sus clips, como en el detalle (SCR-08). Sale del mismo detalle auditado que la pantalla ya pide
 * para el aviso de estado: no hay otra lectura. Cada clip es un `BlurredClipPlayer`, que pide su
 * URL solo al pulsar reproducir (BR-APP-58); sin `evidence.read`, el resumen sin clips.
 * Selección explícita de campos (BR-APP-40): nada del objeto recibido se serializa.
 */
import type { ReactNode } from 'react';
import { BlurredClipPlayer } from '@/components/BlurredClipPlayer/BlurredClipPlayer';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
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
  return (
    <section aria-label={text('scr08.detail.summary')} data-part="finding_evidence">
      <h2>{text('scr08.detail.summary')}</h2>
      <dl>
        {zone !== null && <Field name={text('scr08.labels.zone')}>{zone}</Field>}
        <Field name={text('scr08.detail.family')}>{label('predicate_family', detail.family)}</Field>
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
            <ul aria-label={text('scr08.detail.clips')}>
              {clips.map(({ clip, segment }, index) => (
                <li key={clip.clip_id} data-part="clip">
                  <BlurredClipPlayer
                    evidenceId={clip.evidence_id}
                    clock={classifyClock()}
                    label={`${text('scr08.detail.clip')} ${String(index + 1)} · ${label('clip_segment', clip.segment)}${
                      segment === null
                        ? ''
                        : ` · ${text('scr08.detail.record')} ${String(segment + 1)}`
                    }`}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
