// index.js — lets `node --test web/tests/` run every unit test (Node ≥ 22 treats the
// positional argument as a module path, which resolves to this directory index).
// e2e_smoke.mjs is intentionally excluded: it needs a running backend + browser.
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here)
  .filter((f) => f.endsWith('.test.mjs'))
  .sort();
for (const f of files) {
  await import(pathToFileURL(join(here, f)).href);
}
