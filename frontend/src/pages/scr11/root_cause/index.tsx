/**
 * SCR-11, grupo `root_cause` (`/findings/:finding_state_id/root-cause`, `root_cause.analyze`):
 * análisis de causa raíz guiado con asistente (IF-10).
 *
 * SUP-U05-16 crea un análisis nuevo en cada llamada y U-04 no tiene lectura de un análisis ya
 * iniciado, así que el inicio es siempre una acción explícita de la persona, nunca un efecto del
 * montaje. Sin el texto del estándar (`503 loop_standard_text_unavailable`, diferencia 8) la
 * pantalla lo explica y permite reintentar; un hallazgo fuera del alcance responde la pantalla
 * uniforme «no disponible en su alcance».
 *
 * Iniciar no cambia nada que otra pantalla muestre (el hallazgo no cambia hasta la firma), así
 * que la entrada llama al puerto sin pasar por la caché de `state.query`: no pide nada al montar.
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router';

import { ScreenState } from '@/pages/shared/ScreenState';
import {
  correlationIdOf,
  rootCauseFailure,
  rootCausePort,
  type Analysis,
} from '@/ports/scr11RootCause';
import { text } from '@/texts';

import { Workspace } from './Workspace';

type Start =
  | { readonly status: 'idle' | 'pending' }
  | { readonly status: 'started'; readonly analysis: Analysis }
  | { readonly status: 'failed'; readonly error: unknown };

export default function Page() {
  const { finding_state_id = '' } = useParams();
  const [start, setStart] = useState<Start>({ status: 'idle' });

  if (start.status === 'started') {
    return <Workspace key={start.analysis.analysis_id} initial={start.analysis} />;
  }

  const failure = start.status === 'failed' ? rootCauseFailure(start.error) : null;
  if (failure === 'not_available_in_scope') return <ScreenState state="not_available_in_scope" />;
  const correlationId = start.status === 'failed' ? correlationIdOf(start.error) : undefined;
  const pending = start.status === 'pending';
  const run = () => {
    setStart({ status: 'pending' });
    rootCausePort.start.write({ path: { finding_state_id } }).then(
      (analysis) => {
        setStart({ status: 'started', analysis });
      },
      (error: unknown) => {
        setStart({ status: 'failed', error });
      },
    );
  };
  return (
    <main>
      <nav aria-label={text('navigation.scr11')}>
        <Link to={`/findings/${encodeURIComponent(finding_state_id)}`}>
          {text('scr11.back_to_finding')}
        </Link>
      </nav>
      <h1>{text('navigation.scr11')}</h1>
      <p>{text('scr11.intro')}</p>
      {pending ? <ScreenState state="loading" /> : null}
      {failure === 'standard_text_unavailable' ? (
        <p role="alert" data-part="standard-text-unavailable">
          {text('scr11.standard_text_unavailable')}
        </p>
      ) : null}
      {failure === 'unavailable' ? <ScreenState state="unavailable" /> : null}
      {failure === 'error' ? (
        <ScreenState state="error" {...(correlationId === undefined ? {} : { correlationId })} />
      ) : null}
      <button type="button" disabled={pending} onClick={run}>
        {text(pending ? 'scr11.starting' : failure === null ? 'scr11.start' : 'scr11.retry_start')}
      </button>
      <p>{text('scr11.start_new_notice')}</p>
    </main>
  );
}
