/**
 * SCR-01, grupo `login` (público, VIG-321): las tres rutas del inicio de sesión en dos pasos
 * comparten porción; cada una pinta su paso.
 */
import type { ReactNode } from 'react';
import { useLocation } from 'react-router';

import { Enroll } from './Enroll';
import { LoginForm } from './LoginForm';
import { SecondFactor } from './SecondFactor';

export default function Page(): ReactNode {
  const { pathname } = useLocation();
  if (pathname === '/login/second-factor') return <SecondFactor />;
  if (pathname === '/login/enroll') return <Enroll />;
  return <LoginForm />;
}
