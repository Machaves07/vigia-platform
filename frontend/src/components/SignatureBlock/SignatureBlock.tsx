/**
 * UI-16 `SignatureBlock` (BR-APP-47, 43): nombre y licencia profesional del perfil
 * (`Principal.display_name` y `professional_license`, de `GET /me`) como **instantánea** tomada
 * al montar, el texto que se firma y la acción explícita «Firmar».
 *
 * Sin licencia en el perfil, advierte antes de firmar, pero no deshabilita la firma: la regla la
 * decide el servidor (BR-APP-43). Si el servidor responde `loop_signer_license_missing` (U-04 §6,
 * antes `license_required`), muestra cómo pedir al administrador que la registre. La licencia
 * nunca se captura como texto libre: no hay ningún campo editable.
 */
import { useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { text } from '@/texts';

import styles from './SignatureBlock.module.css';

/** Lo que el bloque toma de `Principal`. */
export interface SignerProfile {
  readonly display_name: string;
  readonly professional_license: string | null;
}

export interface SignatureBlockProps {
  readonly principal: SignerProfile;
  readonly textToSign: string;
  /** Recibe la instantánea mostrada; la pantalla decide qué envía. */
  readonly onSign: (snapshot: SignerProfile) => void;
  /** Aviso del servidor sobre la firma. */
  readonly notice?: Notice | undefined;
  /** Hay una firma en curso: evita el doble envío. */
  readonly pending?: boolean;
}

export const SIGNER_LICENSE_MISSING = 'loop_signer_license_missing';

function hasLicense(profile: SignerProfile): boolean {
  return (
    typeof profile.professional_license === 'string' && profile.professional_license.trim() !== ''
  );
}

export function SignatureBlock({
  principal,
  textToSign,
  onSign,
  notice,
  pending = false,
}: SignatureBlockProps) {
  const [snapshot] = useState<SignerProfile>(() => ({
    display_name: principal.display_name,
    professional_license: principal.professional_license,
  }));
  const licensed = hasLicense(snapshot);
  const licenseMissing = notice?.error.detail_code === SIGNER_LICENSE_MISSING;
  const title = text('components_forms.signature.title');
  return (
    <section className={styles.block} aria-label={title}>
      <h3 className={styles.title}>{title}</h3>
      <dl className={styles.profile}>
        <dt>{text('components_forms.signature.name')}</dt>
        <dd data-part="name">{snapshot.display_name}</dd>
        <dt>{text('components_forms.signature.license')}</dt>
        <dd data-part="license">
          {licensed
            ? snapshot.professional_license
            : text('components_forms.signature.license_absent')}
        </dd>
      </dl>
      <figure className={styles.text}>
        <figcaption>{text('components_forms.signature.text_to_sign')}</figcaption>
        <blockquote data-part="text_to_sign">{textToSign}</blockquote>
      </figure>
      {licensed ? null : (
        <p className={styles.warning} role="note" data-part="warning">
          {text('components_forms.signature.warning_no_license')}
        </p>
      )}
      {notice === undefined ? null : <FieldNotice id={`signature-${notice.id}`} notice={notice} />}
      {licenseMissing ? (
        <p className={styles.warning} data-part="guide">
          {text('components_forms.signature.guide_license_missing')}
        </p>
      ) : null}
      <button
        type="button"
        className={styles.button}
        disabled={pending}
        onClick={() => {
          onSign(snapshot);
        }}
      >
        {text('components_forms.signature.sign')}
      </button>
    </section>
  );
}
