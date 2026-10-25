import { startWorld } from './lifecycle';

export default async function setup(): Promise<void> {
  await startWorld();
}
