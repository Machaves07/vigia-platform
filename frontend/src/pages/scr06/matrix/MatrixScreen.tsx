/**
 * Matriz de pases de la sesión de walk-test (IF-13 paso 3; RV-09; BR-APP-84).
 *
 * - Retoma la sesión de la zona y la matriz derivada del catálogo que ya trae el servidor.
 * - La evidencia de un pase es un `clip_id` del selector de clips de comisionamiento de la zona
 *   (U05-H-11): la pantalla no sube archivos ni conoce otra forma de evidencia.
 * - Al registrarse un pase se programa la muestra de exposición del tramo 3a en segundo plano, sin
 *   bloquear la pantalla ni reintentar un envío fallido.
 * - El estado común es `ScreenState`; la lectura lleva UI-20 («actualizado hace X»).
 */
import { useMemo, useState } from 'react';

import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { correlationOf } from '@/components/NoticeStack/notice';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { fieldIssues } from '@/pages/scr06/common/fieldIssues';
import { noticeFromError } from '@/pages/scr06/common/notices';
import { useScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import {
  scr06CommissioningPort,
  type CommissioningClip,
  type CurrentWalkTest,
  type WalkTestPass,
} from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';

import { PassForm, type PassInput } from './PassForm';
import { PassMatrixTable } from './PassMatrixTable';
import { createExposureSampler, nextAnimationFrame, type ExposureOutcome } from './exposure';
import styles from './matrix.module.css';

const CURRENT_WALK_TEST = 'GET /zones/{zone_id}/walk-tests/current';
const COMMISSIONING_CLIPS = 'GET /zones/{zone_id}/commissioning-clips';
/** Clips por página del selector (el máximo de la ruta es 200). */
const CLIP_PAGE_SIZE = 50;

export interface MatrixScreenProps {
  readonly zoneId: string;
  readonly clock: Clock;
  readonly scope?: string;
  readonly concession?: string | null;
  /** Programa el fotograma siguiente de la muestra de exposición (BR-APP-84). */
  readonly nextFrame?: (callback: () => void) => void;
}

export function MatrixScreen({
  zoneId,
  clock,
  scope = 'all',
  concession = null,
  nextFrame = nextAnimationFrame,
}: MatrixScreenProps) {
  const notices = useScreenNotices();
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [evidenceIssue, setEvidenceIssue] = useState<string | null>(null);
  const [exposure, setExposure] = useState<ReadonlyMap<string, 'pending' | ExposureOutcome>>(
    new Map(),
  );

  const sessionQuery = useScreenQuery<CurrentWalkTest>({
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
  const clipsQuery = useScreenQuery<readonly CommissioningClip[]>({
    key: screenKey({
      screen: 'scr06',
      operation: COMMISSIONING_CLIPS,
      scope,
      concession,
      params: { zone_id: zoneId },
    }),
    read: async ({ signal }) => {
      const page = await scr06CommissioningPort.commissioningClips.read(
        { path: { zone_id: zoneId }, query: { limit: CLIP_PAGE_SIZE } },
        signal,
      );
      return page.clips;
    },
    policy: policyFor(COMMISSIONING_CLIPS),
  });

  const session = sessionQuery.data?.session ?? null;
  const sessionId = session?.session_id ?? null;
  const clips = clipsQuery.data ?? [];

  const sampler = useMemo(
    () =>
      sessionId === null
        ? null
        : createExposureSampler({
            clock,
            nextFrame,
            send: (sample) =>
              scr06CommissioningPort.recordExposureSample.write({
                path: { session_id: sessionId },
                body: sample,
              }),
            onOutcome: (passId, outcome) => {
              setExposure((current) => new Map(current).set(passId, outcome));
            },
          }),
    [clock, nextFrame, sessionId],
  );

  const recordPass = useScreenMutation<PassInput, WalkTestPass>({
    write: (input) =>
      scr06CommissioningPort.recordPass.write({
        path: { session_id: sessionId ?? '' },
        body: input,
      }),
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });

  const queryNotice =
    sessionQuery.error === null ? null : noticeFromError(sessionQuery.error, 'scr06-matrix-query');
  const correlationId =
    queryNotice === null ? undefined : (correlationOf(queryNotice) ?? undefined);

  const register = (input: PassInput) => {
    void recordPass
      .run(input)
      .then((pass) => {
        // La muestra se programa al recibir la respuesta del pase, no antes.
        setExposure((current) => new Map(current).set(pass.pass_id, 'pending'));
        sampler?.sample(pass.pass_id);
      })
      .catch((error: unknown) => {
        const problems = fieldIssues(error);
        setEvidenceIssue(problems.get('evidence_ref') ?? null);
        if (problems.size === 0) notices.add(error);
      });
  };

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
      {sessionQuery.status === 'error' ? (
        <ScreenState
          state="error"
          {...(correlationId === undefined ? {} : { correlationId })}
          onRetry={() => {
            void sessionQuery.refresh();
          }}
        />
      ) : session === null ? (
        sessionQuery.status === 'pending' ? (
          <ScreenState state="loading" />
        ) : (
          <ScreenState state="empty" message={text('scr06.matrix.empty')} />
        )
      ) : (
        <>
          <PassMatrixTable
            session={session}
            selectedRowId={selectedRowId}
            onSelectRow={setSelectedRowId}
            exposureOf={(passId) => exposure.get(passId)}
          />
          <PassForm
            rowId={selectedRowId}
            rows={session.rows}
            clips={clips}
            busy={recordPass.status === 'pending'}
            errorKey={evidenceIssue}
            onSubmit={register}
          />
          {clipsQuery.status === 'pending' ? (
            <p className={styles.hint} data-part="clips_loading">
              {text('scr06.matrix.clips.loading')}
            </p>
          ) : null}
          {clipsQuery.status === 'success' && clips.length === 0 ? (
            <p className={styles.hint} data-part="clips_empty">
              {text('scr06.matrix.clips.empty')}
            </p>
          ) : null}
          <RefreshIndicator
            fetchedAt={sessionQuery.fetchedAt}
            refreshPolicy="on_demand"
            isFetching={sessionQuery.isFetching}
            onRefresh={() => {
              void sessionQuery.refresh();
            }}
            clock={clock}
          />
        </>
      )}
    </main>
  );
}
