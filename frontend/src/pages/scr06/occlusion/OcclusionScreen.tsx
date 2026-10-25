/**
 * Prueba de oclusión por cámara (IF-13 paso 5 con la nota v1.3; RV-09).
 *
 * - Registra la prueba (`POST /walk-tests/{session_id}/occlusion-tests`): nace `pending` con
 *   `deadline` = fin + 5 min, y la pantalla la muestra «verificando» con ese plazo.
 * - Al vencer el plazo vuelve a consultar **solo** la sesión (`recheck.ts`), que es donde el
 *   servidor resuelve la prueba; ninguna otra lectura se refresca y, resuelta, no se programa nada.
 * - El estado común es `ScreenState`; la autorización la decide el servidor (BR-APP-43).
 */
import { useState } from 'react';

import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { correlationOf } from '@/components/NoticeStack/notice';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { NO_FIELD_ISSUES, fieldIssues, type FieldIssues } from '@/pages/scr06/common/fieldIssues';
import { noticeFromError } from '@/pages/scr06/common/notices';
import { useCurrentWalkTest } from '@/pages/scr06/common/useCurrentWalkTest';
import { useScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import {
  occlusionTestsOf,
  scr06CommissioningPort,
  type OcclusionTest,
  type OcclusionTestBody,
} from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';

import { EMPTY_OCCLUSION_DRAFT, OcclusionForm, type OcclusionDraft } from './OcclusionForm';
import { OcclusionList } from './OcclusionList';
import { usePendingRecheck } from './recheck';
import styles from './occlusion.module.css';

/** Campos del formulario a los que se ancla un aviso del servidor. */
type AnchoredField = 'camera_id' | 'declared_reason_es';

export interface OcclusionScreenProps {
  readonly zoneId: string;
  readonly clock: Clock;
  readonly scope?: string;
  readonly concession?: string | null;
}

/** Las pruebas de la sesión más la recién registrada si la sesión aún no la trae. */
function mergeTests(
  fromSession: readonly OcclusionTest[],
  recorded: OcclusionTest | null,
): OcclusionTest[] {
  if (recorded === null || fromSession.some((test) => test.test_id === recorded.test_id)) {
    return [...fromSession];
  }
  return [...fromSession, recorded];
}

/** Campo al que se ancla el rechazo: el motivo si se declaró, si no la cámara. */
function anchorOf(body: OcclusionTestBody): AnchoredField {
  return body.declared_reason_es === undefined ? 'camera_id' : 'declared_reason_es';
}

export function OcclusionScreen({
  zoneId,
  clock,
  scope = 'all',
  concession = null,
}: OcclusionScreenProps) {
  const notices = useScreenNotices();
  const query = useCurrentWalkTest(zoneId, scope, concession);
  const [draft, setDraft] = useState<OcclusionDraft>(EMPTY_OCCLUSION_DRAFT);
  const [issues, setIssues] = useState<FieldIssues>(NO_FIELD_ISSUES);
  const [recorded, setRecorded] = useState<OcclusionTest | null>(null);
  const session = query.data?.session ?? null;
  const sessionId = session?.session_id ?? null;

  const record = useScreenMutation<OcclusionTestBody, OcclusionTest>({
    write: (body) => {
      if (sessionId === null) return Promise.reject(new Error('Sin sesión de walk-test.'));
      return scr06CommissioningPort.recordOcclusionTest.write({
        path: { session_id: sessionId },
        body,
      });
    },
    invalidates: { screen: 'scr06', resources: ['walk_tests'] },
  });

  const tests = mergeTests(session === null ? [] : occlusionTestsOf(session), recorded);
  usePendingRecheck({ tests, clock, fetchedAt: query.fetchedAt, refresh: query.refresh });

  const queryNotice = query.error === null ? null : noticeFromError(query.error, 'scr06-query');
  const correlationId =
    queryNotice === null ? undefined : (correlationOf(queryNotice) ?? undefined);

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('navigation.scr06')}</h1>
      <p className={styles.intro}>{text('scr06.occlusion.intro')}</p>
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
          <p className={styles.empty} data-part="session_empty">
            {text('scr06.matrix.empty')}
          </p>
        )
      ) : (
        <>
          <OcclusionForm
            draft={draft}
            onChange={setDraft}
            clock={clock}
            busy={record.status === 'pending'}
            issues={issues}
            noticeFor={(field) => notices.field(field)}
            onSubmit={(body) => {
              setIssues(NO_FIELD_ISSUES);
              record.run(body).then(
                (test) => {
                  setRecorded(test);
                  setDraft(EMPTY_OCCLUSION_DRAFT);
                },
                (error: unknown) => {
                  const problems = fieldIssues(error);
                  setIssues(problems);
                  if (problems.size === 0) notices.add(error, anchorOf(body));
                },
              );
            }}
          />
          <OcclusionList
            tests={tests}
            onDeclare={(test) => {
              setDraft({
                camera_id: test.camera_id,
                started_at: test.started_at,
                ended_at: test.ended_at,
                declared_reason_es: draft.declared_reason_es,
              });
            }}
          />
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
