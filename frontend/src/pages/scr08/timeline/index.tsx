/**
 * SCR-08, grupo `timeline`: la ficha de vida del hallazgo (TASK-585; O-11 y O-12; BR-APP-54 y
 * 77).
 *
 * Una sola lectura auditada (`GET /loop/findings/{finding_state_id}/timeline`), una vez por
 * apertura y nunca por refresco; la jerarquía da la zona horaria de la planta y el nombre de la
 * zona. Errores: fuera de alcance o inexistente, la pantalla uniforme «no disponible en su
 * alcance» (BR-APP-77); sin compuerta legible, la sección degradada del detalle con «reintentar»;
 * lo transitorio, el aviso de UI-19 con su cuenta regresiva y «Repetir».
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import styles from '@/pages/shared/findingLayout.module.css';
import { findingTitle } from '@/pages/shared/findingTitle';
import { ScreenState } from '@/pages/shared/ScreenState';
import { detailClock, detailFailure, detailPort, type Plant } from '@/ports/scr08Detail';
import { timelineNotice, timelinePort } from '@/ports/scr08Timeline';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { TimelineView } from './TimelineView';

const TIMELINE = 'GET /loop/findings/{finding_state_id}/timeline';

/** Número de apertura: cada montaje de la ficha es una apertura con su propia lectura. */
let openings = 0;

function Failure({ error, onRetry }: { readonly error: Error; readonly onRetry: () => void }) {
  const [dismissed, setDismissed] = useState(false);
  const failure = detailFailure(error);
  if (failure === 'not_available_in_scope') return <ScreenState state="not_available_in_scope" />;
  if (failure === 'gate_state_unavailable') {
    return (
      <div role="alert" data-part={failure}>
        <p>{text('scr08.detail.gate_unavailable')}</p>
        <button type="button" onClick={onRetry}>
          {text('scr08.detail.retry')}
        </button>
      </div>
    );
  }
  return (
    <NoticeStack
      notices={dismissed ? [] : [timelineNotice(error)]}
      clock={detailClock()}
      onDismiss={() => {
        setDismissed(true);
      }}
      onRetry={() => {
        setDismissed(false);
        onRetry();
      }}
    />
  );
}

export default function Page() {
  const { principal } = useUiSession();
  const { finding_state_id: findingId = '' } = useParams();
  const [opening] = useState(() => (openings += 1));
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr08', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => detailPort.hierarchy.read({}, signal),
  });
  const timeline = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: TIMELINE,
      scope,
      concession,
      params: { finding_state_id: findingId, opening },
    }),
    policy: policyFor(TIMELINE),
    // Abrir la ficha es la acción explícita que la pide, una vez por apertura (BR-APP-54).
    enabled: true,
    read: ({ signal }) =>
      timelinePort.timeline.read({ path: { finding_state_id: findingId } }, signal),
  });
  const plants: readonly Plant[] = hierarchy.data?.plants ?? [];
  const data = timeline.data;
  const plant = plants.find((candidate) => candidate.plant_id === data?.plant_id);
  const zone = plant?.zones.find((candidate) => candidate.zone_id === data?.zone_id) ?? null;
  return (
    <main className={styles.page}>
      <nav aria-label={text('scr08.timeline.navigation')}>
        <Link to={`/findings/${findingId}`} data-part="detail_link">
          {text('scr08.timeline.back')}
        </Link>
        {' · '}
        <Link to="/findings">{text('scr08.timeline.inbox')}</Link>
      </nav>
      <header className={styles.header}>
        <div className={styles.heading}>
          <h1>{text('scr08.timeline.title')}</h1>
          {data !== undefined && (
            <p data-part="finding_title">{findingTitle(data.family, zone?.name ?? null)}</p>
          )}
        </div>
      </header>
      <p>{text('scr08.timeline.audited')}</p>
      {timeline.error !== null ? (
        <Failure
          error={timeline.error}
          onRetry={() => {
            void timeline.refresh();
          }}
        />
      ) : data === undefined ? (
        <ScreenState state="loading" />
      ) : (
        <TimelineView timeline={data} timeZone={plant?.timezone ?? 'UTC'} />
      )}
    </main>
  );
}
