/**
 * The openalgo-charts commit pin, read from `openalgo-charts.pin` at the repo root.
 *
 * One file, four readers: the CI workflow that clones the library, the frontend
 * build stamp, the golden generators, and the pytest provenance gate. Keeping the
 * value in one place is the point — a second copy is a second thing to forget to
 * bump, and every one of those readers silently does the wrong thing when the pin
 * and the checkout disagree.
 *
 * Format: `#` comments and blank lines are ignored; the value is the first
 * whitespace-delimited token of the first remaining line, and anything after it is
 * a human label.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Repository root, derived from this file's own location (`<root>/scripts/`). */
export const repoRoot = () => dirname(dirname(fileURLToPath(import.meta.url)));

/** Absolute path of the pin file. */
export const pinPath = (root = repoRoot()) => join(root, 'openalgo-charts.pin');

/**
 * The pin as `{ sha, label, text }`.
 *
 * @param {string} [root] repository root
 * @returns {{ sha: string, label: string, text: string }}
 */
export function readPin(root = repoRoot()) {
  const path = pinPath(root);
  let text;
  try {
    text = readFileSync(path, 'utf8');
  } catch (error) {
    throw new Error(`cannot read the library pin at ${path}: ${error.message}`);
  }
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (trimmed === '' || trimmed.startsWith('#')) continue;
    const [sha, ...rest] = trimmed.split(/\s+/);
    if (!/^[0-9a-f]{40}$/.test(sha)) {
      throw new Error(
        `${path}: expected a 40-character commit SHA, found "${sha}". ` +
          'The first non-comment line must start with the pinned commit.',
      );
    }
    return { sha, label: rest.join(' ').replace(/^#\s*/, ''), text };
  }
  throw new Error(`${path} has no pin in it (every line is blank or a comment)`);
}
