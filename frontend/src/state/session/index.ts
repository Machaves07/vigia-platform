/** `state.session` (LC-APP-06): `UiSession`, `Principal`, avisos de vencimiento y puerta de actividad. */
export { ACTIVITY_EVENTS, ACTIVITY_GATE_MS, createActivityGate } from './activity';
export { principalFromMe } from './principal';
export {
  ABSOLUTE_WARNING_BEFORE_MS,
  IDLE_TIMEOUT_MS,
  IDLE_WARNING_BEFORE_MS,
  initialSession,
  isAuthenticated,
  needsReauth,
  sessionReducer,
  type SessionNotice,
} from './reducer';
export { SessionProvider, useFormDraft, useSession, useUiSession } from './SessionProvider';
export { createSessionStore, LOGIN_ROUTE, type SessionStoreOptions } from './store';
