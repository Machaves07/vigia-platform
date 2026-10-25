/**
 * Recaptura del encuadre (A-55): `POST /zones/{zone_id}/framing-recaptures` con la cámara, el
 * momento de la captura en la hora de la planta y el motivo. El servidor marca la regresión con
 * `framing_recaptured` y la matriz completa; la pantalla muestra la causa que devuelve.
 */
import { useId, useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import {
  fleetNotice,
  fleetPort,
  NODE_REASON_LIMITS,
  type FleetNode,
  type FramingRecaptureBody,
  type FramingRegression,
  type HierarchyPlant,
} from '@/ports/scr07Fleet';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { FleetNotice, plantInstant } from '../shared';
import { REASON_ANCHORS } from './Confirmed';

export function FramingRecapture({
  node,
  plant,
}: {
  readonly node: FleetNode;
  readonly plant: HierarchyPlant | undefined;
}) {
  const id = useId();
  const [zoneId, setZoneId] = useState('');
  const [cameraId, setCameraId] = useState('');
  const [capturedAt, setCapturedAt] = useState('');
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [result, setResult] = useState<FramingRegression | null>(null);
  const mutation = useScreenMutation({
    write: (input: { zoneId: string; body: FramingRecaptureBody }) =>
      fleetPort.recaptureFraming.write({ path: { zone_id: input.zoneId }, body: input.body }),
    invalidates: { screen: 'scr07', resources: ['regression', 'nodes'] },
  });
  const notice = fleetNotice(mutation.error, REASON_ANCHORS);
  const instant = plantInstant(capturedAt, plant?.timezone ?? '');
  const edit =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      mutation.reset();
      set(value);
    };
  return (
    <section aria-label={text('scr07.framing.title')}>
      <h2>{text('scr07.framing.title')}</h2>
      <p>{text('scr07.framing.guide')}</p>
      <form
        aria-label={text('scr07.framing.title')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          setAttempted(true);
          if (
            zoneId === '' ||
            cameraId === '' ||
            instant === null ||
            reasonIssueKey(reason, NODE_REASON_LIMITS) !== null
          )
            return;
          void mutation
            .run({
              zoneId,
              body: {
                camera_id: cameraId,
                captured_at: instant,
                reason_es: reason.normalize('NFC'),
              },
            })
            .then(setResult, () => undefined);
        }}
      >
        <FleetNotice notice={notice} field="form" id={`${id}-form`} />
        <label>
          {text('scr07.zone')}
          <select
            name="zone_id"
            required
            value={zoneId}
            aria-invalid={attempted && zoneId === ''}
            onChange={(event) => {
              edit(setZoneId)(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr07.lifecycle.choose_zone')}</option>
            {node.zones.map((zone) => {
              const known = plant?.zones.find((item) => item.zone_id === zone);
              return (
                <option key={zone} value={zone}>
                  {known === undefined ? zone : `${known.name} (${known.code})`}
                </option>
              );
            })}
          </select>
        </label>
        <label>
          {text('scr07.camera')}
          <select
            name="camera_id"
            required
            value={cameraId}
            aria-invalid={attempted && cameraId === ''}
            onChange={(event) => {
              edit(setCameraId)(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr07.framing.choose_camera')}</option>
            {node.cameras.map((camera) => (
              <option key={camera.camera_id} value={camera.camera_id}>
                {camera.code ?? camera.camera_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr07.framing.captured_at')}
          <input
            type="datetime-local"
            name="captured_at"
            required
            value={capturedAt}
            aria-invalid={attempted && instant === null}
            onChange={(event) => {
              edit(setCapturedAt)(event.currentTarget.value);
            }}
          />
        </label>
        <ReasonField
          name="reason_es"
          label={text('scr07.lifecycle.reason')}
          value={reason}
          limits={NODE_REASON_LIMITS}
          showErrors={attempted}
          notice={notice?.field === 'reason' ? notice : undefined}
          onChange={edit(setReason)}
        />
        <button type="submit" disabled={mutation.status === 'pending'}>
          {text('scr07.framing.submit')}
        </button>
      </form>
      {mutation.status === 'success' && result !== null && (
        <p role="status">
          {`${text('scr07.framing.marked')}: ${
            result.cause === null ? text('scr07.unknown') : label('regression_cause', result.cause)
          }`}
        </p>
      )}
    </section>
  );
}
