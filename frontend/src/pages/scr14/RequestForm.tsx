/** Solicitud por planta: validación de forma, nunca autorización local. */
import { fromZonedTime } from 'date-fns-tz';
import { useId, useState } from 'react';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import {
  exportsClock,
  exportNotice,
  exportsPort,
  validRequest,
  type ExportJob,
  type ExportRequest,
  type Plant,
} from '@/ports/scr14Exports';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { useFormDraft } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { Result } from './shared';

function textValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function listValue(value: unknown): readonly string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function ZoneOption({
  zone,
  selected,
  onChange,
  warning,
}: {
  readonly zone: Plant['zones'][number];
  readonly selected: boolean;
  readonly onChange: () => void;
  readonly warning: 'revoked' | 'unknown' | null;
}) {
  const id = useId();
  return (
    <label>
      <span>
        <input
          type="checkbox"
          aria-label={zone.name}
          aria-describedby={selected && warning !== null ? id : undefined}
          checked={selected}
          onChange={onChange}
        />{' '}
        {zone.name}
      </span>
      {selected && warning === 'revoked' && (
        <span role="note" id={id}>
          {text('scr14.revoked')}
        </span>
      )}
      {selected && warning === 'unknown' && (
        <span role="status" id={id}>
          {text('scr14.gates_unknown')}
        </span>
      )}
    </label>
  );
}

export function RequestForm({
  plants,
  scope,
  concession,
  onRequested,
}: {
  readonly plants: readonly Plant[];
  readonly scope: string;
  readonly concession: string | null;
  readonly onRequested: (job: ExportJob) => void;
}) {
  const id = useId();
  // Borrador en memoria (NFR-APP-38): si la sesión se bloquea, vuelve tal cual al reautenticar.
  const [draft, dispatch] = useFormDraft('SCR-14', 'request');
  const plantId = textValue(draft.values.plant_id);
  const zones = listValue(draft.values.zone_ids);
  const from = textValue(draft.values.from);
  const to = textValue(draft.values.to);
  const form: ExportRequest['package_form'] =
    draft.values.package_form === 'with_clips' ? 'with_clips' : 'without_clips';
  const setPlantId = (value: string) => {
    dispatch({ type: 'set_value', field: 'plant_id', value });
  };
  const setZones = (update: (old: readonly string[]) => readonly string[]) => {
    dispatch({ type: 'set_value', field: 'zone_ids', value: update(zones) });
  };
  const setFrom = (value: string) => {
    dispatch({ type: 'set_value', field: 'from', value });
  };
  const setTo = (value: string) => {
    dispatch({ type: 'set_value', field: 'to', value });
  };
  const setForm = (value: ExportRequest['package_form']) => {
    dispatch({ type: 'set_value', field: 'package_form', value });
  };
  const [invalid, setInvalid] = useState(false);
  const plant = plants.find((item) => item.plant_id === plantId);
  // Una sola entrada agregada: veinte observadores individuales superarían el LRU por pantalla.
  const gates = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /zones/{zone_id}/gates',
      scope,
      concession,
      params: { zone_ids: [...zones] },
    }),
    policy: policyFor('GET /zones/{zone_id}/gates'),
    enabled: zones.length > 0,
    read: async ({ signal }) => {
      const warnings: Record<string, 'revoked' | 'unknown' | null> = {};
      for (const zoneId of zones) {
        try {
          const data = await exportsPort.gates.read({ path: { zone_id: zoneId } }, signal);
          warnings[zoneId] = data.usage.status === 'revoked' ? 'revoked' : null;
        } catch (error) {
          if (signal.aborted) throw error;
          warnings[zoneId] = 'unknown';
        }
      }
      return warnings;
    },
  });
  const mutation = useScreenMutation({
    write: (body: ExportRequest) => exportsPort.request.write({ body }),
    invalidates: { screen: 'scr14', resources: ['exports'] },
  });
  const notice = exportNotice(mutation.error);
  const submit = () => {
    let body: ExportRequest;
    try {
      body = {
        plant_id: plantId,
        zone_ids: [...zones],
        period: {
          from: fromZonedTime(from, plant?.timezone ?? 'UTC').toISOString(),
          to: fromZonedTime(to, plant?.timezone ?? 'UTC').toISOString(),
        },
        package_form: form,
      };
    } catch {
      setInvalid(true);
      return;
    }
    if (!validRequest(body)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    void mutation
      .run(body)
      .then((job) => {
        dispatch({ type: 'saved' });
        onRequested(job);
      })
      .catch(() => undefined);
  };
  return (
    <form
      aria-label={text('scr14.request')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h2>{text('scr14.request')}</h2>
      <label>
        {text('scr14.plant')}
        <select
          value={plantId}
          onChange={(event) => {
            setPlantId(event.target.value);
            setZones(() => []);
            mutation.reset();
          }}
        >
          <option value="">{text('scr14.choose')}</option>
          {plants.map((item) => (
            <option key={item.plant_id} value={item.plant_id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      <fieldset aria-describedby={notice?.field === 'zones' ? `${id}-zones` : undefined}>
        <legend>{text('scr14.zones')}</legend>
        {plant?.zones.map((zone) => (
          <ZoneOption
            key={zone.zone_id}
            zone={zone}
            warning={gates.data?.[zone.zone_id] ?? null}
            selected={zones.includes(zone.zone_id)}
            onChange={() => {
              mutation.reset();
              setZones((old) =>
                !old.includes(zone.zone_id)
                  ? [...old, zone.zone_id]
                  : old.filter((value) => value !== zone.zone_id),
              );
            }}
          />
        ))}
        {notice?.field === 'zones' && <FieldNotice id={`${id}-zones`} notice={notice} />}
      </fieldset>
      <fieldset aria-describedby={notice?.field === 'period' ? `${id}-period` : undefined}>
        <legend>{text('scr14.period')}</legend>
        <label>
          {text('scr14.from')}
          <input
            type="datetime-local"
            value={from}
            onChange={(event) => {
              setFrom(event.target.value);
              mutation.reset();
            }}
          />
        </label>
        <label>
          {text('scr14.to')}
          <input
            type="datetime-local"
            value={to}
            onChange={(event) => {
              setTo(event.target.value);
              mutation.reset();
            }}
          />
        </label>
        {notice?.field === 'period' && <FieldNotice id={`${id}-period`} notice={notice} />}
      </fieldset>
      <label>
        {text('scr14.package_form')}
        <select
          value={form}
          onChange={(event) => {
            setForm(event.target.value === 'with_clips' ? 'with_clips' : 'without_clips');
            mutation.reset();
          }}
        >
          {(['with_clips', 'without_clips'] as const).map((value) => (
            <option key={value} value={value}>
              {label('package_form', value)}
            </option>
          ))}
        </select>
      </label>
      {invalid && <p role="alert">{text('scr14.invalid')}</p>}
      <NoticeStack
        notices={notice === null ? [] : [notice]}
        clock={exportsClock()}
        onDismiss={() => {
          mutation.reset();
        }}
        onRetry={submit}
      />
      {mutation.error !== null && notice === null && (
        <Result query={{ status: mutation.status, error: mutation.error, isFetching: false }}>
          {null}
        </Result>
      )}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr14.request')}
      </button>
    </form>
  );
}
