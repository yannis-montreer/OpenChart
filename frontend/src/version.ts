/** The line to show when this page and the server do not speak the same API version, else null. */
export function versionNotice(client: number, server: number): string | null {
  if (server > client) return `This page is older than the server (API ${client} vs ${server}). Reload to update.`;
  if (server < client) {
    return `This server is older than this page (API ${client} vs ${server}). Update OpenChart on the server: indicators cannot be saved until then.`;
  }
  return null;
}
