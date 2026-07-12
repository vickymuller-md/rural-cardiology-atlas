# Offline dependency verification

The v0.2.0 app environment is pinned to Node `22.17.0` and npm `10.9.2`.
The package lock is version 3. Dependency preparation and analytical/app
verification are separate operations.

`@heartland/ui` no longer resolves through Git, SSH, or GitHub during install.
It is a local `file:vendor/heartland-ui` dependency built reproducibly from
commit `7f80b6d262ad3910fee4d170c7c7477eec47b307`. Its offline pack identity and
the actual vendored directory tree are recorded in:

- `vendor/heartland-ui.provenance.json`;
- `vendor/heartland-ui.tree.jsonl`.

The upstream commit declares MIT in `package.json` and `README.md` but contains
no standalone license file. This is a release-review caveat, not silently
filled with an invented license artifact.

After an authorized environment-preparation step has populated a read-only npm
cache from the exact lock, the clean-room install command is:

```bash
npm ci --offline --ignore-scripts
```

The Vercel clean-deploy command is separately lock-bound and may materialize
dependencies from the registry:

```bash
npm ci --ignore-scripts
```

The deploy command is configuration only; it is never used by the
network-denied clean-room procedure.

Then, with outbound network denied:

```bash
npm test
npm run typecheck
npm run build
```

The v0.2.0 release can run tests, typecheck, and a production build from the
lock-bound dependency set. Its version-bound `counties.json`, `summary.json`,
county-universe list, release manifest, and Census 2025 display topology assets
are committed under `public/data/`. The app has no `next/font` remote-font fetch
and no remote topology URL.
