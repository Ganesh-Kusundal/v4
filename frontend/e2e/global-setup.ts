import { execFileSync } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Refuse to run the browser suite against a bundle that is not the current source.
 *
 * The suite loads `/ui/`, which the server reads from `frontend/dist` on disk — so
 * without this, the tests exercise whatever was last built. That is not
 * hypothetical: a deliberately broken `src/tier2.ts` once passed the whole suite,
 * because `npm run e2e` served a stale `dist/`. `npm run e2e` builds first now,
 * and this covers the runs that do not go through it (a bare `npx playwright
 * test`, in CI or on a developer's machine), where a stale bundle would otherwise
 * turn every assertion into a claim about code that is no longer in the tree.
 *
 * Spawned rather than imported so the failure is the stamp's own message, and so
 * this stays a check of the artifact on disk rather than of this process's view of
 * the modules. `frontend/scripts/artifact-stamp.mjs` is the one implementation;
 * nothing here re-derives a hash.
 */
export default function globalSetup(): void {
  const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
  try {
    execFileSync('node', ['scripts/artifact-stamp.mjs', 'check'], {
      cwd: frontend,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    const stderr = (error as { stderr?: Buffer }).stderr?.toString().trim() ?? String(error);
    throw new Error(
      `the E2E suite would run against a stale bundle.\n\n${stderr}\n\n` +
        'Run `npm run build` (or `npm run e2e`, which builds first) and retry.',
    );
  }
}
