/**
 * Caché de consultas con reloj simulado y puertas mutables para las pruebas de `state.query`.
 */
import { createScreenQueryCache, type RefreshGates, type ScreenGraph } from '@/state/query';

import { FakeClock } from './fakeClock';

export interface MutableGates extends RefreshGates {
  visible: boolean;
  platform: boolean;
  active: boolean;
}

function mutableGates(): MutableGates {
  return {
    visible: true,
    platform: true,
    active: true,
    isVisible() {
      return this.visible;
    },
    canReadPlatform() {
      return this.platform;
    },
    isActive() {
      return this.active;
    },
  };
}

export function queryFixture(options: { readonly graph?: ScreenGraph } = {}) {
  const clock = new FakeClock();
  const gates = mutableGates();
  const cache = createScreenQueryCache({ clock, gates, ...options });
  return { clock, gates, cache };
}

/** Deja correr las promesas y los avisos de TanStack Query (macrotareas a 0 ms, sin tope). */
export async function settle(rounds = 3): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
  }
}
