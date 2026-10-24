/**
 * UI-24 `CopyOnceSecret` (BR-APP-12, 50): muestra **una sola vez** los códigos de recuperación,
 * el enlace de invitación o el código de alta del nodo (con su `node_id` y las huellas SHA-256 de
 * la raíz de `vigia-node-ca`), con «copiar» y la confirmación «lo guardé».
 *
 * El valor se toma al montar y vive solo en el estado del componente, que React suelta al
 * desmontar: nunca en almacenamiento del navegador, título, URL ni historial. Al desmontar se
 * elimina además, si la pantalla lo indica con `discard`, la consulta o mutación de TanStack
 * Query que lo trajo, para que no quede en la caché tras salir. La pantalla debe soltar su propia
 * copia al recibir `onAcknowledged`.
 */
import {
  QueryClientContext,
  type MutationKey,
  type QueryClient,
  type QueryKey,
} from '@tanstack/react-query';
import { useContext, useEffect, useState } from 'react';

import { text } from '@/texts';

import styles from './CopyOnceSecret.module.css';

export type SecretKind = 'recovery_codes' | 'invitation_link' | 'enrollment_code';

export interface CopyOnceSecretProps {
  readonly kind: SecretKind;
  /** Códigos de recuperación como lista; enlace o código de alta como texto. */
  readonly value: string | readonly string[];
  /** Solo para `enrollment_code`: el `node_id` y las huellas de la raíz (una o dos). */
  readonly nodeId?: string | undefined;
  readonly fingerprints?: readonly string[] | undefined;
  /** Entradas de la caché que contienen el valor y se eliminan al desmontar. */
  readonly discard?: { readonly queryKey?: QueryKey; readonly mutationKey?: MutationKey };
  readonly onAcknowledged: () => void;
}

function discardFromCache(
  client: QueryClient | undefined,
  discard: CopyOnceSecretProps['discard'],
): void {
  if (client === undefined || discard === undefined) return;
  if (discard.queryKey !== undefined) {
    client.removeQueries({ queryKey: discard.queryKey, exact: true });
  }
  if (discard.mutationKey !== undefined) {
    const mutations = client.getMutationCache();
    for (const mutation of mutations.findAll({ mutationKey: discard.mutationKey, exact: true })) {
      mutations.remove(mutation);
    }
  }
}

export function CopyOnceSecret({
  kind,
  value,
  nodeId,
  fingerprints,
  discard,
  onAcknowledged,
}: CopyOnceSecretProps) {
  const client = useContext(QueryClientContext);
  // Instantánea al montar: un valor nuevo exige montar otro componente (`key`).
  const [mounted] = useState(() => ({ secret: value, client, discard }));
  const [copied, setCopied] = useState(false);

  useEffect(
    () => () => {
      discardFromCache(mounted.client, mounted.discard);
    },
    [mounted],
  );

  const shown = mounted.secret;
  const label = text(`components_forms.secret.${kind}`);
  return (
    <section className={styles.secret} aria-label={label} data-kind={kind}>
      <h3 className={styles.title}>{label}</h3>
      <p className={styles.once}>{text('components_forms.secret.once')}</p>
      {typeof shown === 'string' ? (
        <code className={styles.value} data-part="value">
          {shown}
        </code>
      ) : (
        <ol className={styles.codes} data-part="value">
          {shown.map((code, index) => (
            <li key={index}>
              <code>{code}</code>
            </li>
          ))}
        </ol>
      )}
      {kind === 'enrollment_code' && nodeId !== undefined ? (
        <p className={styles.detail}>
          {text('components_forms.secret.node_id')}: <code data-part="node_id">{nodeId}</code>
        </p>
      ) : null}
      {kind === 'enrollment_code' && fingerprints !== undefined && fingerprints.length > 0 ? (
        <div className={styles.detail}>
          <p className={styles.detail}>{text('components_forms.secret.fingerprints')}</p>
          <ul className={styles.codes} data-part="fingerprints">
            {fingerprints.map((fingerprint) => (
              <li key={fingerprint}>
                <code>{fingerprint}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          onClick={() => {
            const plain = typeof shown === 'string' ? shown : shown.join('\n');
            void navigator.clipboard.writeText(plain).then(() => {
              setCopied(true);
            });
          }}
        >
          {text('components_forms.secret.copy')}
        </button>
        <span role="status">{copied ? text('components_forms.secret.copied') : ''}</span>
        <button type="button" className={styles.button} onClick={onAcknowledged}>
          {text('components_forms.secret.acknowledge')}
        </button>
      </div>
    </section>
  );
}
