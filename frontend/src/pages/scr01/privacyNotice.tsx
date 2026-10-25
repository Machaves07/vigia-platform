/**
 * Texto del aviso de tratamiento (NFR-APP-33): el marcador versionado de
 * `backend/resources/privacy-notice/<versión>.md`, el mismo que sirve U-02, copiado en
 * `privacyNotices.json` (versión → texto); `scripts/checks/privacy_notice.mjs` exige que la copia
 * sea idéntica. Se pinta como texto plano (sin HTML): título, citas y párrafos.
 *
 * Con la sesión pendiente del aviso, `GET /me` responde `privacy_notice_required` y no da la
 * versión vigente; la puerta usa la del `Principal` si ya lo había y, si no, la única publicada.
 */
import type { ReactNode } from 'react';

import published from './privacyNotices.json';

const NOTICES: ReadonlyMap<string, string> = new Map(Object.entries(published));

/** Versión que se pide aceptar: la del `Principal` o, sin él, la única publicada. */
export function noticeVersion(principalVersion: string | null | undefined): string | null {
  if (principalVersion != null) return principalVersion;
  return NOTICES.size === 1 ? ([...NOTICES.keys()][0] ?? null) : null;
}

/** Texto incorporado de `version`, o `null` si esta construcción no lo trae. */
export function noticeText(version: string): string | null {
  return NOTICES.get(version) ?? null;
}

/** Bloques del texto (separados por líneas en blanco) sin marcas de Markdown. */
function blocks(body: string): { readonly heading: boolean; readonly text: string }[] {
  return body
    .split(/\n\s*\n/)
    .map((block) => {
      const lines = block
        .split('\n')
        .map((line) => line.replace(/^\s*>\s?/, '').trim())
        .filter((line) => line !== '');
      const heading = lines.length === 1 && /^#{1,6}\s/.test(lines[0] ?? '');
      const joined = lines
        .join(' ')
        .replace(/^#{1,6}\s+/, '')
        .replace(/\*\*|__|`/g, '');
      return { heading, text: joined };
    })
    .filter((block) => block.text !== '');
}

/** Pinta el aviso como texto plano. */
export function NoticeBody({ body }: { readonly body: string }): ReactNode {
  return blocks(body).map((block, index) =>
    block.heading ? <h2 key={index}>{block.text}</h2> : <p key={index}>{block.text}</p>,
  );
}
