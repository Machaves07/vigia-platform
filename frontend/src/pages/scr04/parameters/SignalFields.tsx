/** Declaración de señales como configuración, sin pedir código ni JSON al administrador. */
import { screenLabel, type Signal } from '@/ports/scr04Parameters';
import { text } from '@/texts';

export function SignalFields({
  signals,
  onChange,
}: {
  readonly signals: readonly Signal[];
  readonly onChange: (signals: Signal[]) => void;
}) {
  const set = (index: number, change: Partial<Signal>) => {
    onChange(signals.map((item, position) => (position === index ? { ...item, ...change } : item)));
  };
  return (
    <>
      {signals.map((signal, index) => (
        <fieldset key={index}>
          <legend>{`${text('scr04.parameters.signals')} ${String(index + 1)}`}</legend>
          {(['signal_id', 'code', 'description_es'] as const).map((field) => (
            <label key={field}>
              {text(`scr04.parameters.${field}`)}
              <input
                value={signal[field]}
                onChange={(event) => {
                  set(index, { [field]: event.target.value });
                }}
              />
            </label>
          ))}
          <label>
            {text('scr04.parameters.signal_role')}
            <select
              value={signal.role}
              onChange={(event) => {
                set(index, { role: event.target.value as Signal['role'] });
              }}
            >
              {(['energy', 'guard', 'start_command', 'auxiliary'] as const).map((role) => (
                <option key={role} value={role}>
                  {screenLabel('signal_role', role)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {text('scr04.parameters.asserted_level')}
            <select
              value={signal.asserted_level}
              onChange={(event) => {
                set(index, { asserted_level: event.target.value as Signal['asserted_level'] });
              }}
            >
              {(['high', 'low'] as const).map((level) => (
                <option key={level} value={level}>
                  {screenLabel('signal_level', level)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {text('scr04.parameters.reader')}
            <input
              value={signal.source.reader}
              onChange={(event) => {
                set(index, { source: { ...signal.source, reader: event.target.value } });
              }}
            />
          </label>
          <label>
            {text('scr04.parameters.channel')}
            <input
              type="number"
              value={signal.source.channel}
              onChange={(event) => {
                set(index, { source: { ...signal.source, channel: Number(event.target.value) } });
              }}
            />
          </label>
          <button
            type="button"
            onClick={() => {
              onChange(signals.filter((_, position) => position !== index));
            }}
          >
            {text('scr04.parameters.remove_signal')}
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        onClick={() => {
          onChange([
            ...signals,
            {
              signal_id: '',
              code: '',
              description_es: '',
              role: 'auxiliary',
              asserted_level: 'high',
              source: { reader: '', channel: 0 },
            },
          ]);
        }}
      >
        {text('scr04.parameters.add_signal')}
      </button>
    </>
  );
}
