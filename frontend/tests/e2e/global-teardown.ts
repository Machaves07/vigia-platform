import { stopWorld } from './lifecycle';

export default async function teardown(): Promise<void> {
  await stopWorld();
}
