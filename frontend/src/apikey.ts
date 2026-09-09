/**
 * API key resolution. FastAPI's GET / route (fastapi_app.py `_root_with_key`)
 * rewrites the `api-key` meta tag in the served index.html at request time;
 * paper/dev servers leave it empty. The /ui/ static mount serves the dist
 * page un-injected, so when the tag comes up empty we read the key off the
 * injected `/` page instead. Top-level await keeps every consumer (WS URL,
 * order headers) keyed before the first connection.
 */
function fromMeta(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="api-key"]')?.content ?? '';
}

let key = fromMeta();

const hydrated: Promise<void> =
  key !== ''
    ? Promise.resolve()
    : fetch(`${location.origin}/`)
        .then((r) => r.text())
        .then((html) => {
          key = /<meta name="api-key" content="([^"]*)"/.exec(html)?.[1] ?? '';
        })
        .catch(() => {
          // Keyless server (paper) — stay clean.
        });

await hydrated;

/** The server's API key, or '' when it runs keyless. */
export function getApiKey(): string {
  return key;
}

/** Headers every mutating request must carry when the server runs keyed. */
export function authHeaders(): Record<string, string> {
  return key === '' ? {} : { 'X-API-Key': key };
}
