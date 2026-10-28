/**
 * Declaración y revisión, con la política y todo el historial anterior a la vista. Desde el lazo de
 * un incerrable, la revisión destacada abre directamente su pestaña (estado de la navegación,
 * VIG-363; la dirección no lleva consultas).
 */
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { useState } from 'react';
import { useLocation } from 'react-router';
import { ClosureScreen } from '../closure/common';
import { Declaration } from './Declaration';
import { Review } from './Review';

/** `true` si la navegación pidió la revisión (`UnclosableTarget` de `LoopNext`). */
function reviewRequested(state: unknown): boolean {
  return typeof state === 'object' && state !== null && 'review' in state && state.review === true;
}

export default function Page() {
  const location = useLocation();
  const [reviewing, setReviewing] = useState(() => reviewRequested(location.state));
  return (
    <ClosureScreen title={label('finding_lifecycle_state', 'unclosable')}>
      {({ context, detail, records, timeZone, onWritten }) => (
        <>
          <nav aria-label={text('scr09.unclosable.title')}>
            <button
              type="button"
              onClick={() => {
                setReviewing(false);
              }}
            >
              {text('scr09.unclosable.title')}
            </button>
            <button
              type="button"
              onClick={() => {
                setReviewing(true);
              }}
            >
              {text('scr09.unclosable.review')}
            </button>
          </nav>
          <div hidden={reviewing}>
            <Declaration
              context={context}
              plantId={detail.plant_id}
              timeZone={timeZone}
              onWritten={onWritten}
            />
          </div>
          <div hidden={!reviewing}>
            <Review
              context={context}
              plantId={detail.plant_id}
              timeZone={timeZone}
              records={records}
              onWritten={onWritten}
            />
          </div>
        </>
      )}
    </ClosureScreen>
  );
}
