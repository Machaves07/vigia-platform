/**
 * FS-APP-05 (PAT-APP-RES-03, PR-APP-17, BR-APP-58): la URL prefirmada vence a mitad de la
 * reproducción.
 *
 * El detalle del hallazgo de SCR-08 con su clip difuminado. La URL que concede la plataforma
 * vence a los segundos que dibuja la semilla (≤ 5 min). Reanudar antes del vencimiento no pide
 * otra; reanudar después pide **una** URL nueva por reanudación, aunque el reproductor dispare
 * varios `play` seguidos, y sigue desde la misma posición.
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import fc from 'fast-check';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { text } from '@/texts';

import { fixture, WAIT } from '../components/scr08/detail_support';
import { scenario } from '../msw/scenario';
import { faults } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { integer, pump, scenarioCase, scenarioClock, settle, traffic } from './harness';

const READ_URL = /^\/evidence\/[^/]+\/read-url$/;

beforeEach(() => {
  // Como el navegador: `play()` despacha `play` en el elemento.
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (
    this: HTMLMediaElement,
  ) {
    this.dispatchEvent(new Event('play'));
    return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockReturnValue(undefined);
});

describe('FS-APP-05 · URL prefirmada vencida a mitad de reproducción', { timeout: 120_000 }, () => {
  it('una URL nueva al reanudar tras vencer, una sola petición por reanudación', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-05',
      fc.record({
        urlSeconds: integer(30, 300),
        positionSeconds: integer(1, 4),
        resumes: integer(1, 3),
        playEventsPerResume: integer(1, 3),
      }),
    );
    const env = await fixture();
    scenarioClock(env.clock);
    server.use(...scenario('fs_app_05_expiring_evidence_url'));
    faults.evidenceUrlSeconds = parameters.urlSeconds;
    const log = traffic();
    env.view();
    await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
    const player = document.querySelector<HTMLElement>('[data-part="clip_gallery"] figure');
    if (player === null) throw new Error('El detalle no tiene reproductor.');
    const video = player.querySelector('video');
    if (video === null) throw new Error('El detalle no tiene <video>.');

    fireEvent.click(within(player).getByRole('button', { name: text('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(faults.issuedUrls[0]);
    }, WAIT);
    expect(log.count('POST', READ_URL)).toBe(1);

    // Se reproduce; antes de vencer, una pausa y una reanudación no piden otra URL.
    video.currentTime = parameters.positionSeconds;
    await pump(env.clock, parameters.urlSeconds * 1_000 - 1_000);
    fireEvent.pause(video);
    fireEvent.play(video);
    await settle();
    expect(log.count('POST', READ_URL)).toBe(1);

    for (let resume = 1; resume <= parameters.resumes; resume += 1) {
      // La URL vence a mitad de la reproducción; la persona pausa y reanuda (varios `play`).
      await pump(env.clock, parameters.urlSeconds * 1_000);
      fireEvent.pause(video);
      act(() => {
        for (let event = 0; event < parameters.playEventsPerResume; event += 1) {
          fireEvent.play(video);
        }
      });
      await waitFor(() => {
        expect(video.getAttribute('src')).toBe(faults.issuedUrls[resume]);
      }, WAIT);
      expect(log.count('POST', READ_URL)).toBe(1 + resume);
      expect(video.currentTime).toBe(parameters.positionSeconds);
    }
    expect(faults.issuedUrls).toHaveLength(1 + parameters.resumes);
    expect(new Set(faults.issuedUrls).size).toBe(faults.issuedUrls.length);
    // Ni el circuito del almacén ni el de la plataforma vieron un fallo.
    expect(env.connection.getSnapshot().connection.state).toBe('online');
  });
});
