/**
 * Acta de alcance (IF-11 paso 3; U-03 «Versión 1.5», D-2) con `StepWizard`: texto del alcance,
 * encuadre de las cámaras declaradas en el catálogo de la zona, declaración del difuminado con su
 * captura (`blur_check_capture`, PNG admitido) y documento firmado (`scope_record`), y envío a
 * `POST /zones/{zone_id}/gates/mounting/scope-record`.
 *
 * La comprobación automática del difuminado ya no está aquí (D-2: es una guarda del cierre del
 * acta de comisionamiento). La declaración y su captura son opcionales en la forma para que su
 * ausencia la responda el servidor (`catalog_blur_not_verified`), que se ancla al formulario; la
 * política de incerrables nunca bloquea el acta (`plant_policy_missing` solo al aprobar el uso).
 */
import { useId, useState } from 'react';
import { DirectUploadField } from '@/components/DirectUploadField/DirectUploadField';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import { gatesPort, type DeclaredCamera, type ScopeRecordBody } from '@/ports/scr05Gates';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { WriteFailure } from '../shared';
import styles from '../scr05.module.css';

interface Framing {
  readonly included: boolean;
  readonly description: string;
  readonly marker: boolean;
}

const NO_FRAMING: Framing = { included: false, description: '', marker: false };

type DocumentRef = Record<string, unknown>;

export function ScopeRecordWizard({
  zoneId,
  plantId,
  cameras,
}: {
  readonly zoneId: string;
  readonly plantId: string;
  /** Cámaras del catálogo vigente de la zona. */
  readonly cameras: readonly DeclaredCamera[];
}) {
  const id = useId();
  const [scopeText, setScopeText] = useState('');
  const [framings, setFramings] = useState<Readonly<Record<string, Framing>>>({});
  const [declared, setDeclared] = useState(false);
  const [capture, setCapture] = useState<DocumentRef | null>(null);
  const [document, setDocument] = useState<DocumentRef | null>(null);
  const mutation = useScreenMutation({
    write: (body: ScopeRecordBody) =>
      gatesPort.scopeRecord.write({ path: { zone_id: zoneId }, body }),
    invalidates: { screen: 'scr05', resources: ['gates', 'transparency'] },
  });

  const framing = (cameraId: string) => framings[cameraId] ?? NO_FRAMING;
  const setFraming = (cameraId: string, change: Partial<Framing>) => {
    setFramings((current) => ({
      ...current,
      [cameraId]: { ...(current[cameraId] ?? NO_FRAMING), ...change },
    }));
  };
  const included = cameras.filter((camera) => framing(camera.camera_id).included);
  const camerasComplete =
    included.length > 0 &&
    included.every((camera) => framing(camera.camera_id).description.trim() !== '');
  const dirty =
    scopeText !== '' ||
    Object.keys(framings).length > 0 ||
    declared ||
    capture !== null ||
    document !== null;

  const body = (): ScopeRecordBody => ({
    scope_text_es: scopeText.trim(),
    cameras: included.map((camera) => ({
      camera_id: camera.camera_id,
      framing_description_es: framing(camera.camera_id).description.trim(),
      reference_marker: framing(camera.camera_id).marker,
    })),
    blur_verification: { declared, capture_document_ref: capture },
    document_ref: document,
  });

  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="scope_record">
      <h2 id={`${id}-title`}>{text('scr05.scope.title')}</h2>
      <p>{text('scr05.scope.intro')}</p>
      <StepWizard
        title={text('scr05.scope.wizard')}
        dirty={dirty}
        steps={[
          {
            id: 'text',
            title: text('scr05.scope.step_text'),
            complete: scopeText.trim() !== '',
            content: (
              <div className={styles.field}>
                <label htmlFor={`${id}-text`}>{text('scr05.scope.text')}</label>
                <textarea
                  id={`${id}-text`}
                  value={scopeText}
                  required
                  aria-required="true"
                  onChange={(event) => {
                    setScopeText(event.target.value);
                  }}
                />
              </div>
            ),
          },
          {
            id: 'cameras',
            title: text('scr05.scope.step_cameras'),
            complete: camerasComplete,
            content:
              cameras.length === 0 ? (
                <p data-part="no_cameras">{text('scr05.scope.no_cameras')}</p>
              ) : (
                <ul className={styles.list}>
                  {cameras.map((camera) => {
                    const current = framing(camera.camera_id);
                    return (
                      <li key={camera.camera_id} data-part="camera">
                        <label className={styles.inline}>
                          <input
                            type="checkbox"
                            checked={current.included}
                            onChange={(event) => {
                              setFraming(camera.camera_id, { included: event.target.checked });
                            }}
                          />
                          {text('scr05.scope.include_camera')} {camera.code}
                        </label>
                        {current.included ? (
                          <>
                            <div className={styles.field}>
                              <label htmlFor={`${id}-framing-${camera.camera_id}`}>
                                {text('scr05.scope.framing')} {camera.code}
                              </label>
                              <textarea
                                id={`${id}-framing-${camera.camera_id}`}
                                value={current.description}
                                required
                                aria-required="true"
                                onChange={(event) => {
                                  setFraming(camera.camera_id, { description: event.target.value });
                                }}
                              />
                            </div>
                            <label className={styles.inline}>
                              <input
                                type="checkbox"
                                checked={current.marker}
                                onChange={(event) => {
                                  setFraming(camera.camera_id, { marker: event.target.checked });
                                }}
                              />
                              {text('scr05.scope.reference_marker')} {camera.code}
                            </label>
                          </>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              ),
          },
          {
            id: 'blur',
            title: text('scr05.scope.step_blur'),
            complete: true,
            content: (
              <div className={styles.section}>
                <p>{text('scr05.scope.blur_intro')}</p>
                <label className={styles.inline}>
                  <input
                    type="checkbox"
                    checked={declared}
                    onChange={(event) => {
                      setDeclared(event.target.checked);
                    }}
                  />
                  {text('scr05.scope.blur_declared')}
                </label>
                <DirectUploadField
                  label={text('scr05.scope.blur_capture')}
                  target={{ unit: 'catalog_document', plantId, kind: 'blur_check_capture' }}
                  onUploaded={(result) => {
                    if (result.unit === 'catalog_document') setCapture({ ...result.document_ref });
                  }}
                />
              </div>
            ),
          },
          {
            id: 'document',
            title: text('scr05.scope.step_document'),
            complete: true,
            content: (
              <DirectUploadField
                label={text('scr05.scope.document')}
                target={{ unit: 'catalog_document', plantId, kind: 'scope_record' }}
                onUploaded={(result) => {
                  if (result.unit === 'catalog_document') setDocument({ ...result.document_ref });
                }}
              />
            ),
          },
        ]}
        summary={
          <dl className={styles.facts} data-part="scope_summary">
            <div className={styles.fact}>
              <dt>{text('scr05.scope.text')}</dt>
              <dd className={styles.declared}>{scopeText}</dd>
            </div>
            <div className={styles.fact}>
              <dt>{text('scr05.scope.summary_cameras')}</dt>
              <dd>{included.map((camera) => camera.code).join(', ')}</dd>
            </div>
            <div className={styles.fact}>
              <dt>{text('scr05.scope.step_blur')}</dt>
              <dd>
                {text(declared ? 'scr05.scope.blur_yes' : 'scr05.scope.blur_no')}
                {' · '}
                {text(
                  capture === null ? 'scr05.scope.capture_missing' : 'scr05.scope.capture_attached',
                )}
              </dd>
            </div>
            <div className={styles.fact}>
              <dt>{text('scr05.scope.step_document')}</dt>
              <dd>
                {text(
                  document === null
                    ? 'scr05.scope.document_missing'
                    : 'scr05.scope.document_attached',
                )}
              </dd>
            </div>
          </dl>
        }
        onSubmit={async () => {
          await mutation.run(body());
        }}
      />
      {mutation.status === 'success' ? (
        <p role="status" data-part="scope_recorded">
          {text('scr05.scope.done')}
        </p>
      ) : null}
      <WriteFailure error={mutation.error} field="scope_record" />
    </section>
  );
}
