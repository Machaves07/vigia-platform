/**
 * RV-20 · Vista de transparencia (IF-19, H-55; BR-APP-21, 22): alcance declarado (texto, cámaras y
 * cobertura mínima), estándares (título y texto declarado), acuerdo vigente con firmantes y fechas
 * y compuertas. Pinta solo esos campos: ningún hallazgo, contador ni dato de gestión, aunque la
 * respuesta los traiga. Los identificadores se usan para relacionar datos y nunca se muestran.
 */
import type { ReactNode } from 'react';
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { gateDisplay, type Transparency } from '@/ports/scr16Transparency';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import styles from '../scr16.module.css';

interface Camera {
  readonly key: string;
  readonly framing: string | null;
  readonly referenceMarker: boolean | null;
  readonly required: boolean;
}

/** Los encuadres del acta llegan como objetos abiertos: solo se leen sus campos conocidos. */
function cameras(scope: Transparency['declared_scope']): Camera[] {
  const required = new Set(requiredCameraIds(scope.minimum_coverage));
  return scope.cameras.map((camera, index) => {
    const id = typeof camera.camera_id === 'string' ? camera.camera_id : null;
    return {
      key: id ?? `camera-${String(index)}`,
      framing:
        typeof camera.framing_description_es === 'string' &&
        camera.framing_description_es.trim() !== ''
          ? camera.framing_description_es
          : null,
      referenceMarker:
        typeof camera.reference_marker === 'boolean' ? camera.reference_marker : null,
      required: id !== null && required.has(id),
    };
  });
}

function requiredCameraIds(coverage: Transparency['declared_scope']['minimum_coverage']) {
  const ids: unknown = coverage?.required_camera_ids;
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : [];
}

function requiredCount(coverage: Transparency['declared_scope']['minimum_coverage']) {
  const count: unknown = coverage?.required_count;
  return typeof count === 'number' && Number.isInteger(count) && count >= 0 ? count : null;
}

function Instant({ instant, timeZone }: { readonly instant: string; readonly timeZone: string }) {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} />;
}

function ScopeSection({ transparency }: { readonly transparency: Transparency }) {
  const scope = transparency.declared_scope;
  const declared = cameras(scope);
  const count = requiredCount(scope.minimum_coverage);
  return (
    <section className={styles.section} aria-labelledby="scr16-scope">
      <h2 id="scr16-scope">{text('scr16.scope.title')}</h2>
      <h3>{text('scr16.scope.text')}</h3>
      {scope.scope_text_es === null ? (
        <p>{text('scr16.scope.no_record')}</p>
      ) : (
        <p className={styles.declared}>{scope.scope_text_es}</p>
      )}
      <h3>{text('scr16.scope.cameras')}</h3>
      {declared.length === 0 ? (
        <p>{text('scr16.scope.no_cameras')}</p>
      ) : (
        <ul className={styles.cameras}>
          {declared.map((camera) => (
            <li key={camera.key} data-part="camera" data-required={String(camera.required)}>
              <p>{camera.framing ?? text('scr16.scope.camera_framing_missing')}</p>
              {camera.referenceMarker === null ? null : (
                <p>
                  {text(
                    camera.referenceMarker
                      ? 'scr16.scope.reference_marker'
                      : 'scr16.scope.no_reference_marker',
                  )}
                </p>
              )}
              {camera.required ? <p>{text('scr16.scope.required_camera')}</p> : null}
            </li>
          ))}
        </ul>
      )}
      <h3>{text('scr16.scope.minimum_coverage')}</h3>
      {count === null ? (
        <p>{text('scr16.scope.no_minimum_coverage')}</p>
      ) : (
        <p data-part="minimum_coverage">
          {text('scr16.scope.minimum_coverage_cameras')} {String(count)}
        </p>
      )}
    </section>
  );
}

function StandardsSection({ transparency }: { readonly transparency: Transparency }) {
  return (
    <section className={styles.section} aria-labelledby="scr16-standards">
      <h2 id="scr16-standards">{text('scr16.standards.title')}</h2>
      {transparency.standards.length === 0 ? (
        <p>{text('scr16.standards.empty')}</p>
      ) : (
        <ul className={styles.standards}>
          {transparency.standards.map((standard) => (
            <li key={`${standard.standard_id}:${String(standard.version)}`} data-part="standard">
              <h3>{standard.title_es}</h3>
              <p className={styles.meta}>
                {text('scr16.standards.version')} {String(standard.version)}
              </p>
              <p className={styles.declared}>{standard.declared_text}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AgreementSection({
  transparency,
  timeZone,
}: {
  readonly transparency: Transparency;
  readonly timeZone: string;
}) {
  const agreement = transparency.current_agreement;
  return (
    <section className={styles.section} aria-labelledby="scr16-agreement">
      <h2 id="scr16-agreement">{text('scr16.agreement.title')}</h2>
      {agreement === null ? (
        <p>{text('scr16.agreement.none')}</p>
      ) : (
        <>
          <dl className={styles.facts}>
            <dt>{text('scr16.agreement.status')}</dt>
            <dd>{label('agreement_status', agreement.status)}</dd>
            <dt>{text('scr16.agreement.created_at')}</dt>
            <dd>
              <Instant instant={agreement.created_at} timeZone={timeZone} />
            </dd>
            <dt>{text('scr16.agreement.approved_at')}</dt>
            <dd>
              {agreement.approved_at === null ? (
                text('scr16.agreement.not_approved')
              ) : (
                <Instant instant={agreement.approved_at} timeZone={timeZone} />
              )}
            </dd>
          </dl>
          <div className={styles.scroll}>
            <table>
              <caption>{text('scr16.agreement.signatories')}</caption>
              <thead>
                <tr>
                  <th scope="col">{text('scr16.agreement.role')}</th>
                  <th scope="col">{text('scr16.agreement.signatory')}</th>
                  <th scope="col">{text('scr16.agreement.confirmed_at')}</th>
                </tr>
              </thead>
              <tbody>
                {agreement.signatories.map((signatory) => (
                  <tr key={`${signatory.user_id}:${signatory.role}`}>
                    <td>{label('role', signatory.role)}</td>
                    <td>{signatory.display_name ?? text('scr16.agreement.unnamed')}</td>
                    <td>
                      {signatory.confirmed_at === null ? (
                        text('scr16.agreement.pending')
                      ) : (
                        <Instant instant={signatory.confirmed_at} timeZone={timeZone} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function GatesSection({
  transparency,
  timeZone,
}: {
  readonly transparency: Transparency;
  readonly timeZone: string;
}) {
  const { gates } = transparency;
  const rows = [
    { key: 'scr16.gates.mounting', gate: gates.mounting },
    { key: 'scr16.gates.usage', gate: gates.usage },
  ];
  return (
    <section className={styles.section} aria-labelledby="scr16-gates">
      <h2 id="scr16-gates">{text('scr16.gates.title')}</h2>
      <GateStateChip gate={gateDisplay(gates)} timeZone={timeZone} />
      <dl className={styles.facts}>
        {rows.map(({ key, gate }) => (
          <div key={key} className={styles.fact}>
            <dt>{text(key)}</dt>
            <dd>
              {label('gate_status', gate.status)}
              {' · '}
              {gate.decided_at === null ? (
                text('scr16.gates.undecided')
              ) : (
                <>
                  {text('scr16.gates.decided_at')}{' '}
                  <Instant instant={gate.decided_at} timeZone={timeZone} />
                </>
              )}
            </dd>
          </div>
        ))}
        <div className={styles.fact}>
          <dt>{text('scr16.gates.resulting_mode')}</dt>
          <dd>{label('zone_mode', gates.resulting_mode)}</dd>
        </div>
      </dl>
    </section>
  );
}

export interface TransparencyViewProps {
  readonly transparency: Transparency;
  /** Zona horaria IANA de la planta de la zona. */
  readonly timeZone: string;
  /** «Confirmar mi firma», solo cuando la respuesta trae `pending_confirmation_for_me`. */
  readonly confirmation?: ReactNode;
  /** `LiveViewLauncher` con su explicación previa. */
  readonly liveView?: ReactNode;
}

export function TransparencyView({
  transparency,
  timeZone,
  confirmation,
  liveView,
}: TransparencyViewProps) {
  return (
    <div className={styles.view} data-part="transparency">
      <ScopeSection transparency={transparency} />
      <StandardsSection transparency={transparency} />
      <AgreementSection transparency={transparency} timeZone={timeZone} />
      <GatesSection transparency={transparency} timeZone={timeZone} />
      {confirmation}
      {liveView}
    </div>
  );
}
