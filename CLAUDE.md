# KiPI

## KiCad Plugin and Content Manager (PCM) listing

KiPI is listed in KiCad's PCM as `com.github.drewsum.kipi`.

- Upstream metadata repository: <https://gitlab.com/kicad/addons/metadata>
  (`packages/com.github.drewsum.kipi/metadata.json`)
- Drew's fork, which merge requests are opened from: <https://gitlab.com/drewmaatman/metadata>

To publish a new release there:

1. The release workflow attaches `metadata.json` to each GitHub release. Its single
   `versions` entry holds that version's `download_url`, `download_sha256` and sizes.
2. Bring the fork's `main` up to date with upstream `main`, and branch from it (the
   upstream CI does not validate properly from `main`).
3. Add the new entry to the top of `versions` in
   `packages/com.github.drewsum.kipi/metadata.json`, keeping the older versions.
4. Validate: `python3 ci/validate-package.py --schema-v2 schema-v2.json --schema-v1
   schema-v1.json com.github.drewsum.kipi <new metadata.json> <old metadata.json>`.
5. Push the branch to the fork and open a merge request against upstream `main`.
