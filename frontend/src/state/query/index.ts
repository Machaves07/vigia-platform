/** `state.query` (LC-APP-11): caché de consultas de la aplicación sobre TanStack Query. */
export {
  createScreenQueryCache,
  MAX_ENTRIES_PER_SCREEN,
  refreshGates,
  type RefreshGates,
  type ScreenQueryCache,
} from '@/state/query/client';
export { RESOURCES, type Invalidation, type Resource, type ScreenGraph } from './invalidation';
export { SCREENS, screenKey, type Screen, type ScreenQueryKey } from './keys';
export {
  FRESHNESS,
  PERIODIC_INTERVAL_MS,
  policyFor,
  READ_POLICIES,
  UNCACHED_READS,
  type QueryPolicy,
  type ReadOperation,
} from './policies';
export {
  screenQueryOptions,
  ScreenQueryProvider,
  useScreenMutation,
  useScreenQuery,
  type ScreenRead,
} from './useScreenQuery';
