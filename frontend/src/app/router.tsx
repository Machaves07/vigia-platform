/**
 * Enrutador (LC-APP-03): React Router en modo de datos con `lazy` por grupo de rutas; todos los
 * grupos de una pantalla caen en su porción `scrNN` (`config/build.ts`, NFR-APP-01, 02).
 *
 * - **Guarda** (BR-APP-03, 77; NFR-APP-23): un grupo sin permiso no carga su porción y pinta
 *   `NotAvailableInScope`, el mismo componente que la ruta que no existe; la interfaz no distingue
 *   «no existe» de «no es suyo». La guarda es comodidad: el servidor autoriza cada petición.
 * - Las rutas se construyen para un `Principal`: quien lo renueva (`GET /me`, cambio de concesión)
 *   construye un enrutador nuevo. Sin `Principal` solo se abren las rutas públicas y `/` lleva al
 *   inicio de sesión.
 * - **Precarga** (PAT-APP-REN-02): tras la primera pintura, una vez, la porción del panel de inicio
 *   y la de las pantallas enlazadas desde él que el usuario puede abrir. Solo código: no hay
 *   cargadores de datos en las rutas, así que precargar nunca dispara una consulta.
 */
import { type ComponentType, type ReactNode, useEffect, useMemo } from 'react';
import { createBrowserRouter, Navigate, type RouteObject, RouterProvider } from 'react-router';

import type { RouteGroup, ScreenDescriptor } from '@/app/descriptor';
import { canEnterGroup, homeScreen, type NavigationPrincipal } from '@/app/navigation';
import { SCREENS } from '@/app/screens';
import { NotAvailableInScope, ScreenState } from '@/pages/shared/ScreenState';

import { guardChunkLoad } from './chunkLoadGuard';

/** Ruta pública a la que lleva `/` sin sesión. */
const LOGIN_PATH = '/login';

function ScreenLoading(): ReactNode {
  return <ScreenState state="loading" />;
}

function permissionsOf(principal: NavigationPrincipal | null): ReadonlySet<string> | null {
  return principal === null ? null : new Set(principal.effective_permissions);
}

function groupRoute(path: string, group: RouteGroup, open: boolean): RouteObject {
  if (!open) return { path, Component: NotAvailableInScope };
  return {
    path,
    lazy: async () => {
      const module = await guardChunkLoad(group.load);
      return { Component: module.default };
    },
  };
}

function rootElement(principal: NavigationPrincipal | null, screens: readonly ScreenDescriptor[]) {
  if (principal === null) return <Navigate to={LOGIN_PATH} replace />;
  const home = homeScreen(principal.assignments, new Set(principal.effective_permissions), screens);
  return home === null ? <NotAvailableInScope /> : <Navigate to={home.path} replace />;
}

export interface AppRoutesOptions {
  readonly screens?: readonly ScreenDescriptor[];
  /** Lo que se pinta mientras llega la porción de la primera ruta. */
  readonly fallback?: ComponentType;
}

/** Árbol de rutas de la aplicación para `principal` (`null`: sin sesión). */
export function createAppRoutes(
  principal: NavigationPrincipal | null,
  { screens = SCREENS, fallback = ScreenLoading }: AppRoutesOptions = {},
): RouteObject[] {
  const permissions = permissionsOf(principal);
  const children: RouteObject[] = [{ index: true, element: rootElement(principal, screens) }];
  for (const screen of screens) {
    for (const group of screen.route_groups) {
      const open = canEnterGroup(group, permissions);
      for (const path of group.paths) children.push(groupRoute(path, group, open));
    }
  }
  children.push({ path: '*', Component: NotAvailableInScope });
  return [{ path: '/', HydrateFallback: fallback, children }];
}

/**
 * Grupos cuyo código se precarga: el de inicio y todos los que el usuario puede abrir de las
 * pantallas enlazadas desde él. Vacío sin panel de inicio.
 */
export function preloadGroups(
  principal: NavigationPrincipal,
  screens: readonly ScreenDescriptor[] = SCREENS,
): RouteGroup[] {
  const permissions = new Set(principal.effective_permissions);
  const home = homeScreen(principal.assignments, permissions, screens);
  if (home === null) return [];
  const homeDescriptor = screens.find((screen) => screen.screen_id === home.screen_id);
  const homeGroup = homeDescriptor?.route_groups.find((group) => group.id === home.group);
  if (homeGroup === undefined) return [];
  const linked = new Set(homeGroup.linked_screens ?? []);
  const groups = screens
    .filter((screen) => linked.has(screen.screen_id))
    .flatMap((screen) => screen.route_groups)
    .filter((group) => canEnterGroup(group, permissions));
  return [homeGroup, ...groups];
}

/** Carga el código de `preloadGroups`; un fallo activa la recarga controlada, sin recargar. */
export async function preloadHomeCode(
  principal: NavigationPrincipal,
  screens: readonly ScreenDescriptor[] = SCREENS,
): Promise<void> {
  await Promise.allSettled(
    preloadGroups(principal, screens).map((group) => guardChunkLoad(group.load)),
  );
}

/** Ejecuta `task` tras la primera pintura (fotograma siguiente y cola de tareas); da su cancelación. */
export function afterFirstPaint(task: () => void): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const frame = requestAnimationFrame(() => {
    timer = setTimeout(task, 0);
  });
  return () => {
    cancelAnimationFrame(frame);
    if (timer !== undefined) clearTimeout(timer);
  };
}

export interface AppRouterProps {
  readonly principal: NavigationPrincipal | null;
  readonly fallback?: ComponentType;
}

/** Monta el enrutador del `principal` y programa la precarga de código de su inicio. */
export function AppRouter({ principal, fallback }: AppRouterProps): ReactNode {
  const router = useMemo(
    () =>
      createBrowserRouter(createAppRoutes(principal, fallback === undefined ? {} : { fallback })),
    [principal, fallback],
  );
  useEffect(() => {
    if (principal === null) return undefined;
    return afterFirstPaint(() => {
      void preloadHomeCode(principal);
    });
  }, [principal]);
  return <RouterProvider router={router} />;
}
