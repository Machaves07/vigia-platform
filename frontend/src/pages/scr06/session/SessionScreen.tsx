/**
 * Consola de comisionamiento: sesión y pasos (IF-13 pasos 1 y 2; RV-09; BR-APP-45).
 *
 * - Retoma la sesión de la zona (`GET /zones/{zone_id}/walk-tests/current`) o, si no hay ninguna,
 *   la abre con la matriz derivada del catálogo (`POST /zones/{zone_id}/walk-tests`).
 * - Una sesión incompleta se reabre con motivo; los pasos se abren y se cierran con las marcas del
 *   servidor y, si hace falta, con una corrección de horas y su motivo.
 * - Sin sesión, también se puede abrir la reejecución por regresión (solo las filas afectadas);
 *   con sesión, la vista en vivo (UI-18 `LiveViewLauncher`, `live_view.open`) acompaña el
 *   walk-test.
 * - El estado común de la pantalla es `ScreenState`; la lectura lleva UI-20 («actualizado hace X»).
 * - La autorización la decide el servidor (BR-APP-43): la pantalla no oculta ninguna acción por
 *   una regla propia, solo muestra lo que el servidor rechazó.
 */
import { useState } from 'react';

import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { correlationOf } from '@/components/NoticeStack/notice';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { TextField } from '@/pages/scr06/common/TextField';
import { NO_FIELD_ISSUES, fieldIssues, type FieldIssues } from '@/pages/scr06/common/fieldIssues';
import { noticeFromError } from '@/pages/scr06/common/notices';
import { useScreenNotices, type ScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import {
  MIN_PASSES_PER_CELL,
  scr06CommissioningPort,
  type CurrentWalkTest,
  type StepKind,
  type WalkTestSession,
  type WalkTestStep,
} from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';

import { HoursPanel } from './HoursPanel';
import { RegressionRerunPanel } from './RegressionRerunPanel';
import { ReopenPanel } from './ReopenPanel';
import { SessionHeader } from './SessionHeader';
import { StepsPanel, type StepCorrection } from './StepsPanel';
import styles from './session.module.css';

const CURRENT_WALK_TEST = 'GET /zones/{zone_id}/walk-tests/current';
/** Ancla de los avisos de la reejecución por regresión. */
const REGRESSION_RERUN = 'regression_rerun';

export interface SessionScreenProps {
  readonly zoneId: string;
  /** Reloj inyectado (PAT-APP-RES-06): lo usan el cronómetro y la pista de actualización. */
  readonly clock: Clock;
  /** Planta del alcance de la consulta (`all` = todas las de mi alcance). */
  readonly scope?: string;
  /** Concesión activa (`X-Vigia-Concession`) o `null`. */
  readonly concession?: string | null;
}

/** Formulario de apertura de la sesión: solo el número de pases por celda (≥ 3 en U-03). */
function OpenSessionPanel({
  onSubmit,
  busy,
  issue,
}: {
  readonly onSubmit: (passesPerCell: number) => void;
  readonly busy: boolean;
  readonly issue: string | null;
}) {
  const [value, setValue] = useState(String(MIN_PASSES_PER_CELL));
  const [showErrors, setShowErrors] = useState(false);
  const parsed = /^\d+$/.test(value.trim()) ? Number.parseInt(value.trim(), 10) : null;
  const errorKey = issue ?? (showErrors && parsed === null ? 'validation.out_of_range' : null);
  return (
    <section className={styles.panel} aria-label={text('scr06.session.open.title')}>
      <h2 className={styles.title}>{text('scr06.session.open.title')}</h2>
      <p className={styles.empty} data-part="session_empty">
        {text('scr06.session.empty')}
      </p>
      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (parsed === null) {
            setShowErrors(true);
            return;
          }
          onSubmit(parsed);
        }}
      >
        <TextField
          label={text('scr06.session.passes_per_cell')}
          hint={text('scr06.session.open.hint')}
          value={value}
          onChange={setValue}
          type="number"
          min={MIN_PASSES_PER_CELL}
          errorKey={errorKey}
          required
        />
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={busy} data-part="open_session">
            {text(busy ? 'scr06.session.open.submitting' : 'scr06.session.open.submit')}
          </button>
        </div>
      </form>
    </section>
  );
}

/** Sesión vigente: cabecera, reapertura, pasos y horas por zona. */
function SessionBody({
  session,
  clock,
  notices,
}: {
  readonly session: WalkTestSession;
  readonly clock: Clock;
  readonly notices: ScreenNotices;
}) {
  const [issues, setIssues] = useState<FieldIssues>(NO_FIELD_ISSUES);
  const sessionId = session.session_id;

  const startStep = useScreenMutation<
    { readonly step_kind: StepKind; readonly responsible_user_id: string },
    WalkTestStep
  >({
    write: (input) =>
      scr06CommissioningPort.startStep.write({ path: { session_id: sessionId }, body: input }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });
  const closeStep = useScreenMutation<
    { readonly step_id: string; readonly correction: StepCorrection | null },
    WalkTestStep
  >({
    write: (input) =>
      scr06CommissioningPort.closeStep.write({
        path: { session_id: sessionId, step_id: input.step_id },
        body: { correction: input.correction },
      }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });
  const reopen = useScreenMutation<{ readonly reason_es: string }, WalkTestSession>({
    write: (input) =>
      scr06CommissioningPort.reopenWalkTest.write({ path: { session_id: sessionId }, body: input }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });

  const busy = startStep.status === 'pending' || closeStep.status === 'pending';

  /**
   * Un error de escritura se muestra donde corresponde: los problemas del espejo del esquema van
   * junto a su campo (y no se repiten en la pila); lo demás, en la pila de avisos, anclado al
   * campo cuando la pantalla sabe cuál es.
   */
  const reportWriteError = (error: unknown, field: string | null) => {
    const problems = fieldIssues(error);
    setIssues(problems);
    if (problems.size === 0) notices.add(error, field);
  };

  return (
    <>
      <SessionHeader session={session} />
      <ReopenPanel
        session={session}
        busy={reopen.status === 'pending'}
        notice={notices.field('reason_es')}
        errorKey={issues.get('reason_es') ?? null}
        onReopen={(reasonEs) => {
          reopen.run({ reason_es: reasonEs }).catch((error: unknown) => {
            reportWriteError(error, 'reason_es');
          });
        }}
      />
      <StepsPanel
        session={session}
        clock={clock}
        busy={busy}
        issues={issues}
        correctionNotice={notices.field('correction.reason_es')}
        onOpen={(input) => {
          startStep.run(input).catch((error: unknown) => {
            reportWriteError(error, null);
          });
        }}
        onClose={(step, correction) => {
          closeStep.run({ step_id: step.step_id, correction }).catch((error: unknown) => {
            reportWriteError(error, correction === null ? null : 'correction.reason_es');
          });
        }}
      />
      <HoursPanel session={session} />
    </>
  );
}

export function SessionScreen({
  zoneId,
  clock,
  scope = 'all',
  concession = null,
}: SessionScreenProps) {
  const notices = useScreenNotices();
  const query = useScreenQuery<CurrentWalkTest>({
    key: screenKey({
      screen: 'scr06',
      operation: CURRENT_WALK_TEST,
      scope,
      concession,
      params: { zone_id: zoneId },
    }),
    read: ({ signal }) =>
      scr06CommissioningPort.currentWalkTest.read({ path: { zone_id: zoneId } }, signal),
    policy: policyFor(CURRENT_WALK_TEST),
  });

  const openSession = useScreenMutation<{ readonly passes_per_cell: number }, WalkTestSession>({
    write: (input) =>
      scr06CommissioningPort.openWalkTest.write({ path: { zone_id: zoneId }, body: input }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });
  const [openIssue, setOpenIssue] = useState<string | null>(null);
  const rerun = useScreenMutation<{ readonly passes_per_cell: number }, WalkTestSession>({
    write: (input) =>
      scr06CommissioningPort.openRegressionRerun.write({ path: { zone_id: zoneId }, body: input }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });
  const [rerunIssue, setRerunIssue] = useState<string | null>(null);

  const queryNotice = query.error === null ? null : noticeFromError(query.error, 'scr06-query');
  const correlationId =
    queryNotice === null ? undefined : (correlationOf(queryNotice) ?? undefined);
  const session = query.data?.session ?? null;

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('navigation.scr06')}</h1>
      <NoticeStack
        notices={notices.notices}
        clock={clock}
        onDismiss={(id) => {
          notices.dismiss(id);
        }}
      />
      {query.status === 'error' ? (
        <ScreenState
          state="error"
          {...(correlationId === undefined ? {} : { correlationId })}
          onRetry={() => {
            void query.refresh();
          }}
        />
      ) : session === null ? (
        query.status === 'pending' ? (
          <ScreenState state="loading" />
        ) : (
          <>
            <OpenSessionPanel
              busy={openSession.status === 'pending'}
              issue={openIssue}
              onSubmit={(passesPerCell) => {
                openSession.run({ passes_per_cell: passesPerCell }).catch((error: unknown) => {
                  const problems = fieldIssues(error);
                  setOpenIssue(problems.get('passes_per_cell') ?? null);
                  if (problems.size === 0) notices.add(error, null);
                });
              }}
            />
            <RegressionRerunPanel
              busy={rerun.status === 'pending'}
              issue={rerunIssue}
              notice={notices.field(REGRESSION_RERUN)}
              onSubmit={(passesPerCell) => {
                setRerunIssue(null);
                rerun.run({ passes_per_cell: passesPerCell }).catch((error: unknown) => {
                  const problems = fieldIssues(error);
                  setRerunIssue(problems.get('passes_per_cell') ?? null);
                  if (problems.size === 0) notices.add(error, REGRESSION_RERUN);
                });
              }}
            />
          </>
        )
      ) : (
        <>
          <SessionBody session={session} clock={clock} notices={notices} />
          <section className={styles.panel} aria-label={text('scr06.live_view.title')}>
            <p className={styles.empty}>{text('scr06.live_view.intro')}</p>
            <LiveViewLauncher zone_id={zoneId} clock={clock} />
          </section>
          <RefreshIndicator
            fetchedAt={query.fetchedAt}
            refreshPolicy="on_demand"
            isFetching={query.isFetching}
            onRefresh={() => {
              void query.refresh();
            }}
            clock={clock}
          />
        </>
      )}
    </main>
  );
}
