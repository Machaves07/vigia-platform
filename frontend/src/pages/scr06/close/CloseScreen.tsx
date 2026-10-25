/**
 * Cierre del acta de comisionamiento (IF-13 paso 6; interfaces U-03 §3.3 y v1.5, precisión b).
 *
 * - `POST /walk-tests/{session_id}/close` con `signatures[{user_id}]`: quien firma con UI-16
 *   `SignatureBlock` y, si los hay, los cofirmantes que se añaden por identificador; el servidor
 *   comprueba que cada uno tenga un rol vigente sobre la zona.
 * - La aceptación de falsas alarmas lleva motivo y solo viaja si se escribe: el servidor la exige
 *   con `catalog_false_alarm_rate_above_threshold`, y ese aviso se ancla al campo.
 * - `installer_measurements` (D-2): `beacon_latency_ms_p95` entre 0 y 10 000 ms se comprueba antes
 *   de enviar; fuera de rango la petición no sale. Las líneas base son opcionales.
 * - Los rechazos del servidor se anclan al requisito que falló (`CloseRequirements`); el acta
 *   devuelta se muestra en el acto, con su página y su documento a demanda.
 */
import { useRef, useState } from 'react';
import { Link } from 'react-router';

import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { correlationOf } from '@/components/NoticeStack/notice';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { ScreenState } from '@/pages/shared/ScreenState';
import { NO_FIELD_ISSUES, fieldIssues, type FieldIssues } from '@/pages/scr06/common/fieldIssues';
import { fill } from '@/pages/scr06/common/format';
import { noticeFromError } from '@/pages/scr06/common/notices';
import { OptionalTextArea } from '@/pages/scr06/common/OptionalTextArea';
import { TextField } from '@/pages/scr06/common/TextField';
import { useCurrentWalkTest } from '@/pages/scr06/common/useCurrentWalkTest';
import { useScreenNotices, type ScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import { RecordView } from '@/pages/scr06/record/RecordView';
import {
  BEACON_LATENCY_MAX_MS,
  BEACON_LATENCY_MIN_MS,
  scr06CommissioningPort,
  type CloseBody,
  type CommissioningRecord,
  type WalkTestSession,
} from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { useScreenMutation } from '@/state/query';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import {
  CloseRequirements,
  FALSE_ALARM_REASON,
  SIGNATURES,
  closeAnchor,
} from './CloseRequirements';
import { installerMeasurements, parseBeaconLatency, type BaselineDraft } from './measurements';
import styles from './close.module.css';

/** Quien firma desde esta sesión: su usuario y el perfil que muestra UI-16. */
export interface CloseSigner extends SignerProfile {
  readonly user_id: string;
}

export interface CloseScreenProps {
  readonly zoneId: string;
  readonly clock: Clock;
  /** Persona de la sesión (`Principal`), o `null` si no hay sesión de interfaz. */
  readonly signer: CloseSigner | null;
  readonly scope?: string;
  readonly concession?: string | null;
}

const BEACON_FIELD = 'installer_measurements.beacon_latency_ms_p95';

/** Filas que se añaden y quitan: `row` es su identidad local (clave de React), nunca viaja. */
interface CosignerRow {
  readonly row: number;
  readonly user_id: string;
}
interface BaselineRow extends BaselineDraft {
  readonly row: number;
}

function CloseForm({
  zoneId,
  session,
  signer,
  onClosed,
  notices,
}: {
  readonly zoneId: string;
  readonly session: WalkTestSession;
  readonly signer: CloseSigner | null;
  readonly onClosed: (record: CommissioningRecord) => void;
  readonly notices: ScreenNotices;
}) {
  const rows = useRef(0);
  const nextRow = () => {
    rows.current += 1;
    return rows.current;
  };
  const [cosigners, setCosigners] = useState<readonly CosignerRow[]>([]);
  const [reason, setReason] = useState('');
  const [beacon, setBeacon] = useState('');
  const [baselines, setBaselines] = useState<readonly BaselineRow[]>([]);
  const [beaconIssue, setBeaconIssue] = useState<string | null>(null);
  const [issues, setIssues] = useState<FieldIssues>(NO_FIELD_ISSUES);
  const sessionId = session.session_id;

  const close = useScreenMutation<CloseBody, CommissioningRecord>({
    write: (body) =>
      scr06CommissioningPort.closeWalkTest.write({ path: { session_id: sessionId }, body }),
    invalidates: { screen: 'scr06', resources: ['walk_tests', 'commissioning_records'] },
  });

  const sign = (signerId: string) => {
    const latency = parseBeaconLatency(beacon);
    if (!latency.ok) {
      setBeaconIssue(latency.key);
      return;
    }
    setBeaconIssue(null);
    setIssues(NO_FIELD_ISSUES);
    const body: CloseBody = {
      signatures: [signerId, ...cosigners.map((row) => row.user_id)].map((user_id) => ({
        user_id: user_id.trim(),
      })),
      ...(reason.trim() === '' ? {} : { false_alarm_acceptance: { reason_es: reason } }),
      installer_measurements: installerMeasurements(latency.value, baselines, session.zone_id),
    };
    close.run(body).then(onClosed, (error: unknown) => {
      const problems = fieldIssues(error);
      setIssues(problems);
      if (problems.size === 0) notices.add(error, closeAnchor(error));
    });
  };

  const textToSign = fill(text('scr06.close.sign_text'), {
    kind: label('walk_test_kind', session.kind),
    version: String(session.catalog_version),
  });

  return (
    <>
      <CloseRequirements zoneId={zoneId} noticeFor={(field) => notices.field(field)} />

      <fieldset className={styles.panel}>
        <legend className={styles.legend}>{text('scr06.close.cosigners.title')}</legend>
        <p className={styles.hint}>{text('scr06.close.cosigners.hint')}</p>
        {cosigners.length === 0 ? (
          <p className={styles.empty}>{text('scr06.close.cosigners.none')}</p>
        ) : (
          <ul className={styles.rows}>
            {cosigners.map((cosigner, index) => (
              <li key={cosigner.row} className={styles.row} data-part="cosigner">
                <TextField
                  label={fill(text('scr06.close.cosigners.user'), { n: String(index + 1) })}
                  value={cosigner.user_id}
                  onChange={(next) => {
                    setCosigners(
                      cosigners.map((item, at) =>
                        at === index ? { ...item, user_id: next } : item,
                      ),
                    );
                  }}
                  errorKey={issues.get(`signatures[${String(index + 1)}].user_id`) ?? null}
                  required
                />
                <span />
                <button
                  type="button"
                  className={styles.secondary}
                  onClick={() => {
                    setCosigners(cosigners.filter((_, at) => at !== index));
                  }}
                >
                  {text('scr06.close.cosigners.remove')}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.secondary}
            onClick={() => {
              setCosigners([...cosigners, { row: nextRow(), user_id: '' }]);
            }}
          >
            {text('scr06.close.cosigners.add')}
          </button>
        </div>
      </fieldset>

      <section className={styles.panel} aria-label={text('scr06.close.false_alarm.title')}>
        <h2 className={styles.subtitle}>{text('scr06.close.false_alarm.title')}</h2>
        <OptionalTextArea
          label={text('scr06.close.false_alarm.reason')}
          hint={text('scr06.close.false_alarm.hint')}
          value={reason}
          onChange={setReason}
          notice={notices.field(FALSE_ALARM_REASON)}
          errorKey={issues.get(FALSE_ALARM_REASON) ?? null}
        />
      </section>

      <fieldset className={styles.panel} data-part="installer_measurements">
        <legend className={styles.legend}>{text('scr06.close.measurements.title')}</legend>
        <p className={styles.hint}>{text('scr06.close.measurements.hint')}</p>
        <TextField
          label={text('scr06.close.measurements.beacon')}
          hint={fill(text('scr06.close.measurements.beacon_hint'), {
            min: String(BEACON_LATENCY_MIN_MS),
            max: String(BEACON_LATENCY_MAX_MS),
          })}
          value={beacon}
          onChange={(value) => {
            setBeacon(value);
            setBeaconIssue(null);
          }}
          type="number"
          min={BEACON_LATENCY_MIN_MS}
          errorKey={beaconIssue ?? issues.get(BEACON_FIELD) ?? null}
          required
        />
        <h3 className={styles.subtitle}>{text('scr06.close.baselines.title')}</h3>
        {baselines.length === 0 ? (
          <p className={styles.empty}>{text('scr06.close.baselines.none')}</p>
        ) : (
          <ul className={styles.rows}>
            {baselines.map((baseline, index) => {
              const path = `installer_measurements.baselines[${String(index)}]`;
              const update = (patch: Partial<BaselineDraft>) => {
                setBaselines(
                  baselines.map((item, at) => (at === index ? { ...item, ...patch } : item)),
                );
              };
              return (
                <li key={baseline.row} className={styles.row} data-part="baseline">
                  <TextField
                    label={fill(text('scr06.close.baselines.camera'), { n: String(index + 1) })}
                    value={baseline.camera_id}
                    onChange={(value) => {
                      update({ camera_id: value });
                    }}
                    errorKey={issues.get(`${path}.camera_id`) ?? null}
                    required
                  />
                  <TextField
                    label={fill(text('scr06.close.baselines.captured_at'), {
                      n: String(index + 1),
                    })}
                    value={baseline.captured_at}
                    onChange={(value) => {
                      update({ captured_at: value });
                    }}
                    errorKey={issues.get(`${path}.captured_at`) ?? null}
                    required
                  />
                  <button
                    type="button"
                    className={styles.secondary}
                    onClick={() => {
                      setBaselines(baselines.filter((_, at) => at !== index));
                    }}
                  >
                    {text('scr06.close.baselines.remove')}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.secondary}
            onClick={() => {
              setBaselines([...baselines, { row: nextRow(), camera_id: '', captured_at: '' }]);
            }}
          >
            {text('scr06.close.baselines.add')}
          </button>
        </div>
      </fieldset>

      {signer === null ? (
        <p className={styles.empty} data-part="no_signer">
          {text('scr06.close.no_signer')}
        </p>
      ) : (
        <SignatureBlock
          principal={signer}
          textToSign={textToSign}
          notice={notices.field(SIGNATURES)}
          pending={close.status === 'pending'}
          onSign={() => {
            sign(signer.user_id);
          }}
        />
      )}
    </>
  );
}

export function CloseScreen({
  zoneId,
  clock,
  signer,
  scope = 'all',
  concession = null,
}: CloseScreenProps) {
  const notices = useScreenNotices();
  const query = useCurrentWalkTest(zoneId, scope, concession);
  const [closed, setClosed] = useState<CommissioningRecord | null>(null);
  const session = query.data?.session ?? null;
  const queryNotice = query.error === null ? null : noticeFromError(query.error, 'scr06-query');
  const correlationId =
    queryNotice === null ? undefined : (correlationOf(queryNotice) ?? undefined);

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('navigation.scr06')}</h1>
      <p className={styles.intro}>{text('scr06.close.intro')}</p>
      <NoticeStack
        notices={notices.notices}
        clock={clock}
        onDismiss={(id) => {
          notices.dismiss(id);
        }}
      />
      {closed !== null ? (
        <>
          <p role="status" data-part="record_closed">
            {text('scr06.close.closed')}{' '}
            <Link
              to={`/zones/${encodeURIComponent(zoneId)}/commissioning/records/${encodeURIComponent(closed.commissioning_record_id)}`}
            >
              {text('scr06.close.open_record')}
            </Link>
          </p>
          <RecordView record={closed} />
        </>
      ) : query.status === 'error' ? (
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
        <CloseForm
          zoneId={zoneId}
          session={session}
          signer={signer}
          onClosed={setClosed}
          notices={notices}
        />
      )}
    </main>
  );
}
