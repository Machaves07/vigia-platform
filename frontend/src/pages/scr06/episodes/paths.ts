/** Direcciones del grupo `episodes` de SCR-06 (sin consultas: los datos nunca van en la URL). */

export function sessionPath(zoneId: string): string {
  return `/zones/${encodeURIComponent(zoneId)}/commissioning`;
}

export function episodesPath(zoneId: string): string {
  return `${sessionPath(zoneId)}/episodes`;
}

export function episodePath(zoneId: string, photoId: string): string {
  return `${episodesPath(zoneId)}/${encodeURIComponent(photoId)}`;
}
