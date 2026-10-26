import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, expect, it, vi } from 'vitest';
import { BlurredClipPlayer } from '@/components/BlurredClipPlayer/BlurredClipPlayer';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { text } from '@/texts';
import { server } from '../msw/server';
import { FakeClock } from '../support/fakeClock';
import { settle } from '../support/queryCache';

afterEach(() => {
  installTransport(null);
});

it('A-7 · BR-APP-58, 66; NFR-APP-34: sin descarga ni captura, URL renovada al vencer 5 minutos', async () => {
  const clock = new FakeClock(Date.parse('2026-01-01T12:00:00Z'));
  installTransport(
    createTransport({ clock, connection: createPortConnection(clock), origin: 'http://localhost' }),
  );
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockReturnValue(undefined);
  let calls = 0;
  const url = () => `https://store.example.test/clip-${String(calls)}.mp4`;
  server.use(
    http.post('http://localhost/evidence/:id/read-url', () => {
      calls++;
      return HttpResponse.json({
        evidence_id: 'synthetic',
        url: url(),
        expires_at: new Date(clock.now() + 300_000).toISOString(),
      });
    }),
  );
  const view = render(
    <BlurredClipPlayer evidenceId="0190a1b2-5555-7000-8000-000000000001" clock={clock} />,
  );
  const video = view.container.querySelector('video');
  if (video === null) throw new Error('Sin reproductor');
  expect(video.getAttribute('controlslist')?.split(/\s+/)).toContain('nodownload');
  expect(video.hasAttribute('disablepictureinpicture')).toBe(true);
  expect(video.hasAttribute('disableremoteplayback')).toBe(true);
  expect(view.container.querySelector('canvas, a, [download]')).toBeNull();
  expect(fireEvent.contextMenu(video)).toBe(false);
  expect(calls).toBe(0);
  fireEvent.click(screen.getByRole('button', { name: text('media.clip.play') }));
  await waitFor(
    () => {
      expect(video.getAttribute('src')).toBe(url());
    },
    { timeout: 10_000 },
  );
  await act(settle);
  clock.advance(299_999);
  fireEvent.play(video);
  expect(calls).toBe(1);
  clock.advance(1);
  fireEvent.play(video);
  await waitFor(
    () => {
      expect(calls).toBe(2);
    },
    { timeout: 10_000 },
  );
  await waitFor(
    () => {
      expect(video.getAttribute('src')).toBe(url());
    },
    { timeout: 10_000 },
  );
  // No se promete impedir capturas del sistema operativo ni difusión fuera de la interfaz.
}, 30_000);
