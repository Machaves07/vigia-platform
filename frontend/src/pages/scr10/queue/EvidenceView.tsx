/** RV-14: solo señales etiquetadas y clips difuminados, sin campos de persona. */
import { BlurredClipPlayer } from '@/components/BlurredClipPlayer/BlurredClipPlayer';
import { reviewQueuePort, type DetectionEvidence } from '@/ports/scr10ReviewQueue';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';
import { label } from '@/texts/labels';

export function EvidenceView({
  data,
  canViewClips,
  clock,
}: {
  readonly data: DetectionEvidence;
  readonly canViewClips: boolean;
  readonly clock: Clock;
}) {
  return (
    <>
      <h3>{text('scr10.signals')}</h3>
      {data.detection.signals.length === 0 && <p>{text('scr10.no_signals')}</p>}
      <ul aria-label={text('scr10.signals')}>
        {data.detection.signals.map((signal, index) => (
          <li key={index}>
            {label('signal_role', signal.role)}: {label('signal_value', signal.value)}
            {' · '}
            {text(signal.valid ? 'scr10.valid' : 'scr10.invalid')}
          </li>
        ))}
      </ul>
      {data.clips.length === 0 && <p>{text('scr10.no_clips')}</p>}
      {canViewClips &&
        data.clips.map((clip) => (
          <BlurredClipPlayer
            key={clip.evidence_id}
            evidenceId={clip.evidence_id}
            clock={clock}
            requestUrl={(evidence_id) => reviewQueuePort.clip.write({ path: { evidence_id } })}
          />
        ))}
    </>
  );
}
