/** Campos expresos de RV-19, sin posibilidad estructural de editar supresiones o inmediato. */
import { useId } from 'react';
import type { Preferences, PreferenceInput } from '@/ports/scr15Notifications';
import { text } from '@/texts';

export function PreferencesFields({
  data,
  draft,
  disabled = false,
  onChange,
}: {
  readonly data: Preferences;
  readonly draft: PreferenceInput;
  readonly disabled?: boolean;
  readonly onChange: (value: PreferenceInput) => void;
}) {
  const prefix = useId();
  return (
    <>
      <fieldset disabled={disabled}>
        <legend>{text('scr15.email_settings')}</legend>
        {(['email_enabled_normal', 'email_enabled_digest'] as const).map((field) => (
          <div key={field}>
            <label htmlFor={`${prefix}-${field}`}>{text(`scr15.${field}`)}</label>
            <input
              id={`${prefix}-${field}`}
              type="checkbox"
              checked={draft[field]}
              onChange={(event) => {
                onChange({ ...draft, [field]: event.target.checked });
              }}
            />
          </div>
        ))}
        <label htmlFor={`${prefix}-weekday`}>{text('scr15.weekday')}</label>
        <select
          id={`${prefix}-weekday`}
          value={draft.weekly_digest_weekday}
          onChange={(event) => {
            onChange({ ...draft, weekly_digest_weekday: Number(event.target.value) });
          }}
        >
          {[1, 2, 3, 4, 5, 6, 7].map((day) => (
            <option key={day} value={day}>
              {text(`scr15.weekday_${String(day)}`)}
            </option>
          ))}
        </select>
      </fieldset>
      <dl>
        <dt>{text('scr15.hard_bounce')}</dt>
        <dd>{text(data.email_suppressed_hard_bounce ? 'scr15.yes' : 'scr15.no')}</dd>
        <dt>{text('scr15.complaint')}</dt>
        <dd>{text(data.email_suppressed_complaint ? 'scr15.yes' : 'scr15.no')}</dd>
      </dl>
    </>
  );
}
