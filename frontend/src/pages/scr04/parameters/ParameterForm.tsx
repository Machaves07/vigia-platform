/** Borrador en memoria; validación espejo antes de cada escritura, sin reglas de autorización. */
import { useId, useState } from 'react';
import { ReasonField } from '@/components/ReasonField/ReasonField';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import {
  bodyIssues,
  PARAMETER_OPERATIONS,
  parameterReasonLimits,
  parametersPort,
  reasonValid,
  screenLabel,
  type Camera,
  type Catalog,
  type ParameterKind,
  type Signal,
} from '@/ports/scr04Parameters';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Failure } from './shared';
import { SignalFields } from './SignalFields';
import styles from './parameters.module.css';

const KINDS: readonly ParameterKind[] = [
  'cameras',
  'minimum_coverage',
  'signals',
  'thresholds',
  'windows',
];
const emptyCamera = (): Camera => ({
  camera_id: '',
  code: '',
  role_in_zone: 'primary',
  declared_min_fps: 1,
  stream_reference: '',
});

function object(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function initialNumbers(initial: Catalog) {
  const numericText = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value) ? String(value) : '';
  const thresholds = object(initial.catalog.thresholds);
  const coverage = object(initial.catalog.minimum_coverage);
  const clip = object(initial.catalog.clip_window);
  const episode = object(initial.catalog.episode);
  return {
    review: numericText(thresholds.review),
    publication: numericText(thresholds.publication),
    required_count: numericText(coverage.required_count),
    pre_seconds: numericText(clip.pre_seconds),
    post_seconds: numericText(clip.post_seconds),
    grouping_window_ms: numericText(episode.grouping_window_ms),
    max_segment_ms: numericText(episode.max_segment_ms),
  };
}

function initialCameras(initial: Catalog): Camera[] {
  if (!Array.isArray(initial.catalog.cameras)) return [emptyCamera()];
  return initial.catalog.cameras.map((value: unknown) => {
    const camera = object(value);
    return {
      camera_id: typeof camera.camera_id === 'string' ? camera.camera_id : '',
      code: typeof camera.code === 'string' ? camera.code : '',
      role_in_zone: camera.role_in_zone === 'redundant' ? 'redundant' : 'primary',
      declared_min_fps: typeof camera.declared_min_fps === 'number' ? camera.declared_min_fps : 1,
      stream_reference: '',
    };
  });
}

function initialSignals(initial: Catalog): Signal[] {
  if (!Array.isArray(initial.catalog.signals)) return [];
  return initial.catalog.signals.map((value: unknown) => {
    const signal = object(value);
    const source = object(signal.source);
    const role = signal.role;
    return {
      signal_id: typeof signal.signal_id === 'string' ? signal.signal_id : '',
      code: typeof signal.code === 'string' ? signal.code : '',
      description_es: typeof signal.description_es === 'string' ? signal.description_es : '',
      role: role === 'energy' || role === 'guard' || role === 'start_command' ? role : 'auxiliary',
      asserted_level: signal.asserted_level === 'low' ? 'low' : 'high',
      source: {
        reader: typeof source.reader === 'string' ? source.reader : '',
        channel: typeof source.channel === 'number' ? source.channel : 0,
      },
    };
  });
}

export function ParameterForm({
  zoneId,
  initial,
}: {
  readonly zoneId: string;
  readonly initial: Catalog;
}) {
  const id = useId();
  const [kind, setKind] = useState<ParameterKind>('thresholds');
  const [numbers, setNumbers] = useState(() => initialNumbers(initial));
  const [cameras, setCameras] = useState<Camera[]>(() => initialCameras(initial));
  const [cameraIds, setCameraIds] = useState(() => {
    const ids = object(initial.catalog.minimum_coverage).required_camera_ids;
    return Array.isArray(ids)
      ? ids.filter((value): value is string => typeof value === 'string').join('\n')
      : '';
  });
  const [signals, setSignals] = useState<Signal[]>(() => initialSignals(initial));
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<Catalog | null>(null);
  const mutation = useScreenMutation({
    write: async (input: { kind: ParameterKind; body: Record<string, unknown> }) => {
      const path = { zone_id: zoneId };
      // Cada operación mantiene su cuerpo generado; el discriminante solo selecciona el puerto.
      switch (input.kind) {
        case 'cameras':
          return parametersPort.cameras.write({
            path,
            body: input.body as Parameters<typeof parametersPort.cameras.write>[0]['body'],
          });
        case 'minimum_coverage':
          return parametersPort.minimum_coverage.write({
            path,
            body: input.body as Parameters<typeof parametersPort.minimum_coverage.write>[0]['body'],
          });
        case 'signals':
          return parametersPort.signals.write({
            path,
            body: input.body as Parameters<typeof parametersPort.signals.write>[0]['body'],
          });
        case 'thresholds':
          return parametersPort.thresholds.write({
            path,
            body: input.body as Parameters<typeof parametersPort.thresholds.write>[0]['body'],
          });
        case 'windows':
          return parametersPort.windows.write({
            path,
            body: input.body as Parameters<typeof parametersPort.windows.write>[0]['body'],
          });
      }
    },
    invalidates: { screen: 'scr04', resources: ['catalog', 'regression'] },
  });
  const numeric = (name: keyof typeof numbers) => (
    <label key={name}>
      {text(`scr04.parameters.${name}`)}
      <input
        name={name}
        type="number"
        step="any"
        value={numbers[name]}
        aria-describedby={issue === null ? undefined : `${id}-issue`}
        aria-invalid={issue !== null}
        onChange={(event) => {
          setNumbers({ ...numbers, [name]: event.target.value });
        }}
      />
    </label>
  );
  const submit = async () => {
    if (mutation.status === 'pending') return;
    setAttempted(true);
    setIssue(null);
    setReceipt(null);
    mutation.reset();
    if (!reasonValid(reason)) return;
    const body: Record<string, unknown> = { reason_es: reason.normalize('NFC') };
    const number = (name: keyof typeof numbers) =>
      numbers[name].trim() === '' ? NaN : Number(numbers[name]);
    switch (kind) {
      case 'cameras':
        body.cameras = cameras;
        break;
      case 'minimum_coverage':
        Object.assign(body, {
          required_count: number('required_count'),
          required_camera_ids: cameraIds.trim() === '' ? [] : cameraIds.trim().split(/\s+/u),
        });
        break;
      case 'signals':
        body.signals = signals;
        break;
      case 'thresholds':
        Object.assign(body, { review: number('review'), publication: number('publication') });
        if (number('review') >= number('publication')) {
          setIssue('scr04.parameters.threshold_order');
          return;
        }
        break;
      case 'windows':
        Object.assign(body, {
          clip_window: { pre_seconds: number('pre_seconds'), post_seconds: number('post_seconds') },
          episode: {
            grouping_window_ms: number('grouping_window_ms'),
            max_segment_ms: number('max_segment_ms'),
          },
        });
        break;
    }
    const issues = bodyIssues(PARAMETER_OPERATIONS[kind], body);
    if (issues.length > 0) {
      setIssue(issues[0]?.key ?? 'validation.invalid');
      return;
    }
    try {
      setReceipt(await mutation.run({ kind, body }));
      setReason('');
      setAttempted(false);
    } catch {
      /* El aviso del servidor prevalece y el borrador se conserva. */
    }
  };
  return (
    <form
      aria-label={text('scr04.parameters.title')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      className={styles.form}
    >
      <fieldset disabled={mutation.status === 'pending'}>
        <legend>{text('scr04.parameters.title')}</legend>
        <label>
          {text('scr04.parameters.parameter')}
          <select
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as ParameterKind);
              setIssue(null);
              mutation.reset();
            }}
          >
            {KINDS.map((item) => (
              <option key={item} value={item}>
                {text(`scr04.parameters.${item}`)}
              </option>
            ))}
          </select>
        </label>
        {kind === 'thresholds' && (
          <>
            {numeric('review')}
            {numeric('publication')}
          </>
        )}
        {kind === 'minimum_coverage' && (
          <>
            {numeric('required_count')}
            <label>
              {text('scr04.parameters.required_camera_ids')}
              <textarea
                value={cameraIds}
                onChange={(event) => {
                  setCameraIds(event.target.value);
                }}
              />
            </label>
          </>
        )}
        {kind === 'windows' && (
          <>
            {numeric('pre_seconds')}
            {numeric('post_seconds')}
            {numeric('grouping_window_ms')}
            {numeric('max_segment_ms')}
          </>
        )}
        {kind === 'signals' && <SignalFields signals={signals} onChange={setSignals} />}
        {kind === 'cameras' && (
          <>
            <p>{text('scr04.parameters.stream_help')}</p>
            {cameras.map((camera, index) => (
              <fieldset key={index}>
                <legend>{`${text('scr04.parameters.cameras')} ${String(index + 1)}`}</legend>
                {(['camera_id', 'code', 'declared_min_fps', 'stream_reference'] as const).map(
                  (field) => (
                    <label key={field}>
                      {text(`scr04.parameters.${field}`)}
                      <input
                        type={field === 'declared_min_fps' ? 'number' : 'text'}
                        value={camera[field]}
                        onChange={(event) => {
                          setCameras(
                            cameras.map((item, position) =>
                              position === index
                                ? {
                                    ...item,
                                    [field]:
                                      field === 'declared_min_fps'
                                        ? Number(event.target.value)
                                        : event.target.value,
                                  }
                                : item,
                            ),
                          );
                        }}
                      />
                    </label>
                  ),
                )}
                <label>
                  {text('scr04.parameters.role_in_zone')}
                  <select
                    value={camera.role_in_zone}
                    onChange={(event) => {
                      setCameras(
                        cameras.map((item, position) =>
                          position === index
                            ? {
                                ...item,
                                role_in_zone: event.target.value as Camera['role_in_zone'],
                              }
                            : item,
                        ),
                      );
                    }}
                  >
                    {(['primary', 'redundant'] as const).map((role) => (
                      <option value={role} key={role}>
                        {screenLabel('camera_role_in_zone', role)}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  onClick={() => {
                    setCameras(cameras.filter((_, position) => position !== index));
                  }}
                >
                  {text('scr04.parameters.remove_camera')}
                </button>
              </fieldset>
            ))}
            <button
              type="button"
              onClick={() => {
                setCameras([...cameras, emptyCamera()]);
              }}
            >
              {text('scr04.parameters.add_camera')}
            </button>
          </>
        )}
        <ReasonField
          label={text('scr04.parameters.reason')}
          value={reason}
          onChange={setReason}
          limits={parameterReasonLimits}
          showErrors={attempted}
        />
        <Failure error={mutation.error} />
        {mutation.error === null && issue !== null && (
          <p id={`${id}-issue`} role="alert">
            {text(issue)}
          </p>
        )}
        <button type="submit">{text('scr04.parameters.save')}</button>
      </fieldset>
      {receipt !== null && (
        <WriteReceipt
          record={`${text('scr04.parameters.version')} ${String(receipt.catalog_version)}`}
          roleInUse={receipt.role_in_use}
        />
      )}
    </form>
  );
}
