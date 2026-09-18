// Write `trading/tests/analytics/goldens/PROVENANCE.json`, describing the fixture
// set as it exists on disk right now.
//
// Its own step, run last by `scripts/regenerate-goldens.sh`, and deliberately not
// called from the generators: each generator writes one part of the fixture set,
// so a record written by whichever ran last would describe a directory that only
// that generator had finished updating — and would list only itself, quietly
// claiming the rest of the goldens were captured when they were not. One writer,
// run after all of them, is the only version of this that can be true.
//
// Consequently a generator run on its own leaves the record stale, and
// `trading/tests/analytics/test_goldens_provenance.py` fails: the fix is to
// regenerate everything, which is the intent.
import { resolveLibrary } from './library-dist.mjs';
import { writeProvenance } from './goldens-provenance.mjs';

// Every step `scripts/regenerate-goldens.sh` runs, in order. Kept here rather than
// scraped from the shell script so the record cannot claim a step that no longer
// exists without someone editing this line.
const GENERATORS = [
  'generate_fixtures.py',
  'generate_goldens.mjs',
  'generate_goldens_transforms.mjs',
  'generate_goldens_profiles.mjs',
];

// `kinds: []` on purpose: this step captures nothing from the library, it only
// records which build was used. Resolving it still verifies the pin and the
// freshness of `dist/`, which is the whole point — a record naming a checkout that
// was never checked against its own build would be worthless.
const library = resolveLibrary({ kinds: [] });
const record = writeProvenance(library, { generators: GENERATORS });
console.log(JSON.stringify(record, null, 1));
