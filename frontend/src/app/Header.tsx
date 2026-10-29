/**
 * UI-02 `Header` (BR-APP-05 y 17): marca, UI-03 `ScopeSelector`, la campana de SCR-15 (relevo de
 * VIG-290 que sustituye VIG-318) y el menú de cuenta con sesiones, contraseña, segundo factor y
 * cerrar sesión.
 *
 * - El menú de cuenta (`openAccount`) es una lista de enlaces que se abre con su botón y se cierra
 *   con Escape, al elegir una entrada o al volver a pulsar el botón.
 * - «Cerrar sesión» (`logout`) es `state.session`: `POST /auth/logout` y, aunque falle, borra la
 *   caché, `UiPreferences`, la selección de planta, la concesión y todo lo que hay en memoria.
 * - La cabecera no muestra el nombre ni el correo de la persona; sí el rol con el que trabaja.
 * - Sistema visual v1 (VIG-364): una fila de 60 px (64 en tableta) con los elementos separados.
 */
import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router';

import { ScopeSelector } from '@/components/ScopeSelector/ScopeSelector';
import { Bell } from '@/pages/scr15/Bell';
import type { ScopeStore } from '@/state/scope';
import { useSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import styles from './AppShell.module.css';

/** Entradas del menú de cuenta, en este orden; llevan a su sección de `/account` (SCR-01). */
const ACCOUNT_LINKS = [
  { section: 'sessions', key: 'app_shell.account.sessions' },
  { section: 'password', key: 'app_shell.account.password' },
  { section: 'second-factor', key: 'app_shell.account.second_factor' },
] as const;

function AccountMenu({ role }: { readonly role: string | null }): ReactNode {
  const session = useSession();
  const id = useId();
  const [open, setOpen] = useState(false);
  const close = () => {
    setOpen(false);
  };
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <div className={styles.account}>
      {role === null ? null : (
        <span className={styles.accountRole} data-part="role">
          {label('role', role)}
        </span>
      )}
      <button
        type="button"
        className={styles.button}
        aria-expanded={open}
        aria-controls={`${id}-menu`}
        onClick={() => {
          setOpen((value) => !value);
        }}
      >
        {text('app_shell.account.menu')}
      </button>
      <ul id={`${id}-menu`} className={styles.accountMenu} hidden={!open}>
        <li>
          <Link className={styles.link} to="/account" onClick={close} data-section="account">
            {text('navigation.menu.account')}
          </Link>
        </li>
        {ACCOUNT_LINKS.map((entry) => (
          <li key={entry.section}>
            <Link
              className={styles.link}
              to={`/account#${entry.section}`}
              onClick={close}
              data-section={entry.section}
            >
              {text(entry.key)}
            </Link>
          </li>
        ))}
        <li>
          <button
            type="button"
            className={styles.button}
            onClick={() => {
              close();
              void session.logout();
            }}
          >
            {text('app_shell.account.logout')}
          </button>
        </li>
      </ul>
    </div>
  );
}

export function Header({
  scope,
  role = null,
}: {
  readonly scope: ScopeStore;
  /** Rol con el que la persona trabaja (el de su inicio), que se muestra junto a «Cuenta». */
  readonly role?: string | null;
}): ReactNode {
  return (
    <header className={styles.header}>
      <Link className={styles.brand} to="/">
        {text('common.app_name')}
      </Link>
      <div className={styles.headerScope}>
        <ScopeSelector store={scope} />
      </div>
      <div className={styles.headerActions}>
        <Bell />
        <AccountMenu role={role} />
      </div>
    </header>
  );
}
