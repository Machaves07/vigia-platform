/** Usa TODAS las capacidades del navegador real; solo sustituye el destino tras la puerta. */
import { browserProbe, H264_TYPE, missingCapabilities, start } from '../../src/bootstrap/check';

const video = document.createElement('video');
let reactVersion: string | null = null;
const loading = start({
  document,
  loadApp: async () => {
    const react = await import('react');
    reactVersion = react.version;
  },
});
await loading;
const output = document.createElement('output');
output.textContent = JSON.stringify({
  secure: globalThis.isSecureContext,
  descriptor: H264_TYPE,
  native: video.canPlayType(H264_TYPE),
  incomplete: video.canPlayType('video/mp4; codecs="avc1"'),
  explicit: video.canPlayType('video/mp4; codecs="avc1.42E01E"'),
  webm: video.canPlayType('video/webm; codecs="vp8"'),
  missing: missingCapabilities(browserProbe()),
  loaded: loading !== null,
  reactVersion,
});
document.body.append(output);
