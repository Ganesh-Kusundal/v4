/**
 * The build stamp for `frontend/dist`, and the check that reads it back.
 *
 * Why this exists: `frontend/dist` is not committed — the FastAPI host serves it
 * from disk at `/ui`, and the browser suite loads it — so *what is on disk* and
 * *what the sources say* can disagree, and every test then passes against code
 * nobody is editing. That happened here: `npm run e2e` used to test without
 * building, and a deliberately broken source file still passed, because the suite
 * was exercising the previous bundle.
 *
 * `npm run e2e` now builds first, which covers the one path a developer takes.
 * This stamp covers the others — a bare `npx playwright test`, a server started
 * from a stale dist, a CI job that skipped a step — by recording enough for a
 * reader to tell whether the bundle it is about to trust was made from the tree
 * on disk:
 *
 *   inputsHash    the sources the bundle is a function of: `src/`, `index.html`,
 *                 the build config and lockfile, and the pinned chart library
 *                 (`openalgo-charts.pin`, plus its `dist/` hash when the checkout
 *                 is present). Content hashes, so a `git checkout` that rewrites
 *                 mtimes without changing bytes does not read as stale.
 *   distHash      the tree hash of what the build emitted, excluding this file.
 *   files         per-file `sha256:<hex>` (prefixed, like every other hash in
 *                 this repository), so a caller — `trading/scripts/e2e_smoke.py`
 *                 — can check one served response body against the stamp without
 *                 re-implementing the tree hash.
 *
 * `check` fails when either hash disagrees with disk — i.e. when the bundle is not
 * the one the sources build to — and names the fix.
 *
 * Usage, from `frontend/`:
 *   node scripts/artifact-stamp.mjs write   # after `vite build`
 *   node scripts/artifact-stamp.mjs check   # before trusting or serving dist
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hashFile, hashTree, walkFiles } from '../../scripts/hash-tree.mjs';
import { readPin, repoRoot } from '../../scripts/pin.mjs';

/** `frontend/`, derived from this file's own location (`frontend/scripts/`). */
const FRONTEND = dirname(dirname(fileURLToPath(import.meta.url)));
const DIST = join(FRONTEND, 'dist');
const STAMP = join(DIST, 'BUILD_STAMP.json');

/**
 * What the bundle is a function of: the sources, the html entry point, the build
 * config, and the lockfile — a dependency bump changes the output without touching
 * `src/`, so it has to be an input or the stamp would miss it. `node_modules/`
 * itself is the lockfile's business.
 */
const INPUTS = [
  'src',
  'index.html',
  'vite.config.ts',
  'package.json',
  'package-lock.json',
  'tsconfig.json',
];

/** The library checkout, for the dist hash: `$OPENALGO_CHARTS_ROOT`, else the repo. */
const libraryRoot = () =>
  process.env.OPENALGO_CHARTS_ROOT ?? join(repoRoot(), 'openalgo-charts');

/** `sha256:<hex>` of one file's bytes — the same form `hashTree` reports. */
const sha256 = (path) => hashFile(path) ?? '';

/** Every regular file under `dir`, `/`-separated and sorted — see hash-tree.mjs. */
const filesUnder = (dir) => (existsSync(dir) ? walkFiles(dir) : []);

/**
 * Every input file as `[label, path]`, whether the entry names a directory or one
 * file. Labels are what the hash is fed, so `src/main.ts` and a stray `main.ts`
 * at `frontend/` cannot collide.
 */
function inputFiles() {
  const out = [];
  for (const entry of INPUTS) {
    const path = join(FRONTEND, entry);
    if (!existsSync(path)) continue;
    if (statSync(path).isDirectory()) {
      for (const rel of filesUnder(path)) out.push([`${entry}/${rel}`, join(path, rel)]);
    } else {
      out.push([entry, path]);
    }
  }
  return out.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

/**
 * The hash of the sources `dist` claims to be a build of.
 *
 * The library is folded in by pin and, when the checkout is there, by build hash:
 * a host bundle built against a different chart library is a different artifact
 * even though every file under `frontend/` is identical, and that is precisely the
 * kind of drift (pin bumped, build forgotten) this is meant to catch.
 */
function inputsHash() {
  const digest = createHash('sha256');
  const files = inputFiles();
  if (files.length === 0) {
    throw new Error(`no build inputs found under ${FRONTEND} — is this the frontend directory?`);
  }
  for (const [label, path] of files) {
    digest.update(label);
    digest.update('\0');
    digest.update(readFileSync(path));
    digest.update('\0');
  }
  const pin = readPin(repoRoot());
  digest.update(pin.sha);
  digest.update('\0');
  const libDist = join(libraryRoot(), 'dist');
  if (existsSync(libDist)) {
    digest.update(hashTree(libDist) ?? '');
    digest.update('\0');
  }
  return `sha256:${digest.digest('hex')}`;
}

/** The per-file hashes of the built bundle, excluding the stamp itself. */
function distFiles() {
  const out = {};
  for (const rel of filesUnder(DIST)) {
    if (rel === 'BUILD_STAMP.json') continue;
    out[rel] = sha256(join(DIST, rel));
  }
  return out;
}

/** `{ inputsHash, distHash, files, … }` as it is on disk right now. */
function snapshot() {
  const pin = readPin(repoRoot());
  const library = libraryRoot();
  return {
    inputsHash: inputsHash(),
    distHash: hashTree(DIST, { exclude: (rel) => rel === 'BUILD_STAMP.json' }),
    files: distFiles(),
    library: { pin: pin.sha, label: pin.label, distHash: existsSync(join(library, 'dist')) ? hashTree(join(library, 'dist')) : null },
    builtAt: new Date().toISOString(),
  };
}

const readStamp = () => {
  if (!existsSync(STAMP)) {
    throw new Error(
      `${STAMP} does not exist — frontend/dist is not a stamped build.\n` +
        '  cd frontend && npm run build   (which writes the stamp)',
    );
  }
  return JSON.parse(readFileSync(STAMP, 'utf8'));
};

function write() {
  if (!existsSync(DIST)) {
    throw new Error(`${DIST} does not exist — run \`vite build\` before stamping`);
  }
  const record = snapshot();
  writeFileSync(STAMP, `${JSON.stringify(record, null, 1)}\n`);
  const count = Object.keys(record.files).length;
  console.log(
    `stamped ${count} file${count === 1 ? '' : 's'} in dist from openalgo-charts ${record.library.pin.slice(0, 7)}`,
  );
  return record;
}

/**
 * Fail unless `frontend/dist` is the build of the sources on disk.
 *
 * @returns {object} the stamp, so a caller can read `files`/`distHash` off it
 *   rather than hashing dist a second time.
 */
export function check() {
  const stamped = readStamp();
  const current = snapshot();

  if (current.inputsHash !== stamped.inputsHash) {
    throw new Error(
      'frontend/dist was built from different sources than the ones on disk.\n' +
        `  stamped: ${stamped.inputsHash}\n  on disk: ${current.inputsHash}\n` +
        '  The bundle at frontend/dist is stale — a test or a server pointed at it\n' +
        '  would be exercising code that is no longer in the tree.\n' +
        '  Fix: cd frontend && npm run build',
    );
  }
  if (current.distHash !== stamped.distHash) {
    throw new Error(
      'frontend/dist has been modified since it was built.\n' +
        `  stamped: ${stamped.distHash}\n  on disk: ${current.distHash}\n` +
        '  Fix: cd frontend && npm run build',
    );
  }
  return stamped;
}

// `write` and `check` as a CLI; importing this file only gets `check`.
if (process.argv[2] === 'write') {
  write();
} else if (process.argv[2] === 'check') {
  const record = check();
  console.log(
    `dist is a build of the current sources (openalgo-charts ${record.library.pin.slice(0, 7)}, ` +
      `${Object.keys(record.files).length} files)`,
  );
} else if (process.argv[2] !== undefined) {
  console.error('usage: node scripts/artifact-stamp.mjs [write|check]');
  process.exit(2);
}
