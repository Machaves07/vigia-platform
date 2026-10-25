/** RV-19: el borrador solo nace tras GET; supresiones fuera del cuerpo editable. */
import { useState } from 'react';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  notificationsPort,
  type Preferences,
  type PreferenceInput,
} from '@/ports/scr15Notifications';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { PreferencesFields } from './PreferencesFields';

export function PreferencesForm({ data }: { readonly data: Preferences }) {
  const [draft, setDraft] = useState<PreferenceInput>(() => ({
    email_enabled_normal: data.email_enabled_normal,
    email_enabled_digest: data.email_enabled_digest,
    weekly_digest_weekday: data.weekly_digest_weekday,
  }));
  const mutation = useScreenMutation({
    write: (body: PreferenceInput) => notificationsPort.savePreferences.write({ body }),
    invalidates: { screen: 'scr15', resources: ['notification_preferences'] },
  });
  return (
    <form
      aria-label={text('scr15.preferences')}
      onSubmit={(event) => {
        event.preventDefault();
        void mutation.run(draft).catch(() => undefined);
      }}
    >
      <PreferencesFields
        data={data}
        draft={draft}
        disabled={mutation.status === 'pending'}
        onChange={(value) => {
          setDraft(value);
          mutation.reset();
        }}
      />
      {mutation.error !== null && <ScreenState state="error" />}
      {mutation.status === 'success' && <p role="status">{text('scr15.saved')}</p>}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr15.save')}
      </button>
    </form>
  );
}
