import { screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { SCREENS } from '@/app/screens';
import { text } from '@/texts';
import { mountShell, serveSession, shellMe, WAIT } from '../support/appShell';
import { effectivePermissions } from '../generators';

it.each(['classify', 'loop/closure', 'loop/unclosable'])(
  'A-9 · BR-APP-01, 03: mando sin acciones de clasificar o cerrar y URL %s no disponible',
  async (action) => {
    const permissions = effectivePermissions('customer_organization', ['line_manager']);
    expect(permissions).not.toContain('findings.classify');
    expect(permissions).not.toContain('findings.close_and_sign');
    serveSession(shellMe(permissions, { role: 'line_manager' }));
    mountShell(`/findings/0190a1b2-2222-7000-8000-000000000001/${action}`, SCREENS);
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('button', { name: /clasificar|cerrar/i })).toBeNull();
    expect(screen.queryByRole('link', { name: text('navigation.scr09') })).toBeNull();
  },
  30_000,
);
