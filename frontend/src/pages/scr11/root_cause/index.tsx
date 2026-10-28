/**
 * SCR-11, grupo `root_cause` (`/findings/:finding_state_id/root-cause`, `root_cause.analyze`):
 * análisis de causa raíz guiado con asistente (IF-10).
 *
 * Al entrar, la pantalla lee el análisis en curso del hallazgo (VIG-363,
 * `GET …/root-cause-analyses/in-progress`, sin caché): si hay uno guardado sin terminar muestra su
 * estado, sus porqués y si tiene un borrador del asistente, y «Continuar» lo retoma con ese
 * borrador marcado; «Iniciar» solo aparece si no hay ninguno, porque SUP-U05-16 crea un análisis
 * nuevo en cada llamada (el inicio es siempre una acción explícita de la persona). Sin el texto
 * del estándar (`503 loop_standard_text_unavailable`, diferencia 8) la pantalla lo explica y
 * permite reintentar; un hallazgo fuera del alcance responde la pantalla uniforme «no disponible
 * en su alcance».
 */
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router';

import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  correlationIdOf,
  plantTimeZone,
  rootCauseFailure,
  rootCausePort,
  type Analysis,
  type AnalysisInProgress,
} from '@/ports/scr11RootCause';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { Workspace } from './Workspace';

type Start =
  | { readonly status: 'idle' | 'pending' }
  | {
      readonly status: 'started';
      readonly analysis: Analysis;
      readonly resumeRequestId: string | null;
    }
  | { readonly status: 'failed'; readonly error: unknown };

type Current =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly value: AnalysisInProgress }
  | { readonly status: 'failed'; readonly error: unknown };

/** El análisis en curso del hallazgo, leído al entrar y sin caché (lo último guardado). */
function useInProgress(findingId: string, attempt: number): Current {
  const key = `${findingId}:${String(attempt)}`;
  const [result, setResult] = useState<{ readonly key: string; readonly current: Current } | null>(
    null,
  );
  useEffect(() => {
    const controller = new AbortController();
    rootCausePort.inProgress
      .read({ path: { finding_state_id: findingId } }, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) setResult({ key, current: { status: 'ready', value } });
        },
        (error: unknown) => {
          if (!controller.signal.aborted) setResult({ key, current: { status: 'failed', error } });
        },
      );
    return () => {
      controller.abort();
    };
  }, [findingId, key]);
  // Cada intento (otro hallazgo o «repetir») está cargando hasta que llega su respuesta.
  return result?.key === key ? result.current : { status: 'loading' };
}

/** El análisis guardado sin terminar: su estado, sus porqués y el borrador, con «Continuar». */
function InProgress({
  analysis,
  hasDraft,
  onContinue,
}: {
  readonly analysis: Analysis;
  readonly hasDraft: boolean;
  readonly onContinue: () => void;
}) {
  const { principal } = useUiSession();
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr11',
      operation: 'GET /hierarchy',
      scope: principal?.organization.organization_id ?? 'none',
      concession: principal?.concession_id ?? null,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => rootCausePort.hierarchy.read({}, signal),
  });
  const timeZone = plantTimeZone(hierarchy.data, analysis.zone_id) ?? 'UTC';
  return (
    <section aria-label={text('scr11.in_progress')} data-part="in-progress">
      <h2>{text('scr11.in_progress')}</h2>
      <p>{text('scr11.in_progress_intro')}</p>
      <dl>
        <dt>{text('scr11.state')}</dt>
        <dd data-part="analysis-state">{label('root_cause_analysis_state', analysis.state)}</dd>
        <dt>{text('scr11.saved_at')}</dt>
        <dd>
          <TimestampWithOffset instant={analysis.updated_at} timeZone={timeZone} />
        </dd>
      </dl>
      <h3>{text('scr11.summary_whys')}</h3>
      {analysis.why_steps.length === 0 ? (
        <p>{text('scr11.no_whys')}</p>
      ) : (
        <ol data-part="summary-whys">
          {analysis.why_steps.map((step) => (
            <li key={step.step_index}>
              {step.statement}
              {step.category === null ? null : ` (${label('cause_category', step.category)})`}
            </li>
          ))}
        </ol>
      )}
      <p data-part="draft-notice">
        {text(hasDraft ? 'scr11.in_progress_draft' : 'scr11.in_progress_no_draft')}
      </p>
      <button type="button" onClick={onContinue}>
        {text('scr11.continue')}
      </button>
    </section>
  );
}

export default function Page() {
  const { finding_state_id = '' } = useParams();
  const [attempt, setAttempt] = useState(0);
  const current = useInProgress(finding_state_id, attempt);
  const [start, setStart] = useState<Start>({ status: 'idle' });

  if (start.status === 'started') {
    return (
      <Workspace
        key={start.analysis.analysis_id}
        initial={start.analysis}
        resumeRequestId={start.resumeRequestId}
      />
    );
  }

  const readFailure = current.status === 'failed' ? rootCauseFailure(current.error) : null;
  const failure = start.status === 'failed' ? rootCauseFailure(start.error) : readFailure;
  if (failure === 'not_available_in_scope') return <ScreenState state="not_available_in_scope" />;
  const correlationId =
    start.status === 'failed'
      ? correlationIdOf(start.error)
      : current.status === 'failed'
        ? correlationIdOf(current.error)
        : undefined;
  const pending = start.status === 'pending';
  const run = () => {
    setStart({ status: 'pending' });
    rootCausePort.start.write({ path: { finding_state_id } }).then(
      (analysis) => {
        setStart({ status: 'started', analysis, resumeRequestId: null });
      },
      (error: unknown) => {
        setStart({ status: 'failed', error });
      },
    );
  };
  const saved = current.status === 'ready' ? current.value.analysis : null;
  return (
    <main>
      <nav aria-label={text('navigation.scr11')}>
        <Link to={`/findings/${encodeURIComponent(finding_state_id)}`}>
          {text('scr11.back_to_finding')}
        </Link>
      </nav>
      <h1>{text('navigation.scr11')}</h1>
      <p>{text('scr11.intro')}</p>
      {current.status === 'loading' || pending ? <ScreenState state="loading" /> : null}
      {failure === 'standard_text_unavailable' ? (
        <p role="alert" data-part="standard-text-unavailable">
          {text('scr11.standard_text_unavailable')}
        </p>
      ) : null}
      {failure === 'unavailable' ? <ScreenState state="unavailable" /> : null}
      {failure === 'error' ? (
        <ScreenState
          state="error"
          {...(correlationId === undefined ? {} : { correlationId })}
          {...(start.status === 'failed'
            ? {}
            : {
                onRetry: () => {
                  setAttempt((value) => value + 1);
                },
              })}
        />
      ) : null}
      {saved !== null && current.status === 'ready' ? (
        <InProgress
          analysis={saved}
          hasDraft={current.value.assistant_request_id !== null}
          onContinue={() => {
            setStart({
              status: 'started',
              analysis: saved,
              resumeRequestId: current.value.assistant_request_id,
            });
          }}
        />
      ) : null}
      {current.status === 'ready' && saved === null ? (
        <>
          <button type="button" disabled={pending} onClick={run}>
            {text(
              pending
                ? 'scr11.starting'
                : start.status === 'failed'
                  ? 'scr11.retry_start'
                  : 'scr11.start',
            )}
          </button>
          <p>{text('scr11.start_new_notice')}</p>
        </>
      ) : null}
    </main>
  );
}
