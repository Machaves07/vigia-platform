/**
 * Campana de la cabecera (SCR-15): relevo de VIG-290 que importa la cabecera de VIG-320 y que
 * VIG-318 sustituye por la de las no leídas. El relevo no llama a la red ni muestra ningún número:
 * solo enlaza la lista de notificaciones.
 */
import type { ReactNode } from 'react';
import { Link } from 'react-router';

import { text } from '@/texts';

export function Bell(): ReactNode {
  return <Link to="/notifications">{text('navigation.bell')}</Link>;
}
