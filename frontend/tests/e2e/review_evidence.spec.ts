import { authenticatedTest as test, expect } from './fixtures';

test.use({ role: 'coordinator_sst', actionTimeout: 30_000 });
test.skip(({ hasTouch }) => hasTouch, 'Escritorio');

test('la cola abre señales y reproduce el clip real de la detección', async ({
  authenticatedPage: page,
}) => {
  await page.goto('/review-queue');
  const row = page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: 'Resolver', exact: true }) })
    .first();
  const records = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/ledger/records',
  );
  await row.getByRole('button', { name: 'Abrir clip y señales' }).click();
  const response = await records;
  expect(response.status()).toBe(200);
  // La consulta del expediente usa el tipo persistido, sin ampliar la ventana de recepción.
  expect(new URL(response.url()).searchParams.get('record_type')).toBe(
    'detection_for_review_received',
  );
  await expect(page.getByRole('list', { name: 'Señales en el instante' })).toBeVisible();
  const evidence = page.getByRole('region', { name: 'Clip y señales de la detección' });
  await expect(evidence.locator('time')).toHaveCount(0);
  const play = page.getByRole('button', { name: 'Reproducir clip', exact: true }).first();
  await expect(play).toBeVisible();
  const grant = page.waitForResponse(/\/evidence\/[^/]+\/read-url$/);
  await play.click();
  expect((await grant).status()).toBe(200);
  const video = page.locator('video').first();
  await expect
    .poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime))
    .toBeGreaterThan(0);
  expect(
    await video.evaluate((element: HTMLVideoElement) => element.error?.code ?? null),
  ).toBeNull();
  await video.evaluate((element: HTMLVideoElement) => {
    element.pause();
  });
});
