/**
 * UI-02 `Header` (BR-APP-05 y 17): marca, UI-03 `ScopeSelector`, la campana de SCR-15 (relevo de
 * VIG-290 que sustituye VIG-318) y el menú de cuenta con sesiones, contraseña, segundo factor y
 * cerrar sesión.
 *
 * - El menú de cuenta (`openAccount`) es una lista de enlaces que se abre con su botón y se cierra
 *   con Escape, al elegir una entrada o al volver a pulsar el botón.
 * - «Cerrar sesión» (`logout`) es `state.session`: `POST /auth/logout` y, aunque falle, borra la
 *   caché, `UiPreferences`, la selección de planta, la concesión y todo lo que hay en memoria.
 * - La cabecera no muestra el nombre ni el correo de la persona.
 */
import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router';

import { ScopeSelector } from '@/components/ScopeSelector/ScopeSelector';
import { Bell } from '@/pages/scr15/Bell';
import type { ScopeStore } from '@/state/scope';
import { useSession } from '@/state/session';
import { text } from '@/texts';

import styles from './AppShell.module.css';

/** Entradas del menú de cuenta, en este orden; llevan a su sección de `/account` (SCR-01). */
const ACCOUNT_LINKS = [
  { section: 'sessions', key: 'app_shell.account.sessions' },
  { section: 'password', key: 'app_shell.account.password' },
  { section: 'second-factor', key: 'app_shell.account.second_factor' },
] as const;

function AccountMenu(): ReactNode {
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

export function Header({ scope }: { readonly scope: ScopeStore }): ReactNode {
  return (
    <header className={styles.header}>
      <Link className={styles.brand} to="/">
        {text('common.app_name')}
      </Link>
      <ScopeSelector store={scope} />
      <div className={styles.headerActions}>
        <Bell />
        <AccountMenu />
      </div>
    </header>
  );
}
