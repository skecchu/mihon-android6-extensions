# Mihon extensions for Android 6

A daily job that takes the [Keiyoushi](https://github.com/keiyoushi/extensions) manga
source extensions and converts them so they install and run on **Android 6** (API 23),
then publishes them as a Mihon extension store.

It is meant for a Mihon build patched to run on Android 6. Stock Mihon needs Android 8, and
so do Keiyoushi's own APKs (minSdk 26, dex 038, v2-only signatures).

## Add the store in Mihon

Settings > Browse > Extension stores > Add:

    https://raw.githubusercontent.com/skecchu/mihon-android6-extensions/repo/index.json

## How a conversion works

No source code is built and nothing is changed by hand. For each extension,
`scripts/a6conv.py` takes the `.jar` (plain class files) that Keiyoushi publishes next to
every APK and:

1. dexes it with D8 for API 23, with core library desugaring, against the exact library
   versions Mihon 0.21.3 ships (`scripts/libs.py` pins them by SHA-256; Mihon's own
   `source-api` and `core/common` classes are in `lib/`);
2. adds a trimmed desugared Java library (`j$.*`, built with L8) holding only the classes and
   members the extension calls;
3. adds the interface companion classes (`$-CC`) of Mihon's libraries, which D8's output
   calls but Mihon's R8 removed from the app;
4. sets `minSdkVersion` to 23 and `versionCode` to Keiyoushi's code × 100 + converter
   revision in the binary manifest, keeping every other resource;
5. zipaligns and signs with v1+v2+v3 signatures.

This relies on Mihon loading extensions child-first (`DelegateLastClassLoader`), so the
bundled `j$` and `$-CC` classes take precedence.

Only extensions whose Keiyoushi version changed are converted on each run. If a
conversion fails, the previous converted version stays in the store; `report.md` on the
`repo` branch lists failures.

## Layout

- `.github/workflows/store.yml`: the daily job (also runs on demand; "full" reconverts everything).
- `scripts/build_store.py`: reads Keiyoushi's `index.json`, converts what changed, writes the store index.
  If `index.json` is missing, unreadable or has no `.jar` links, it reads `index.pb` instead
  (`scripts/storepb.py`), and `report.md` says which one was used.
- `scripts/publish.py`: uploads APKs to the `apks-N` releases, force-pushes the `repo` branch, prunes old APKs.
- `scripts/a6conv.py`, `axml.py`, `dexrefs.py`: the converter.

APKs live in releases `apks-0` to `apks-3` (a release holds at most 1000 files); the
store index is `index.json` on the `repo` branch.

## Secrets

- `KEYSTORE_B64`: the signing keystore, base64. Its key alias must be `ext`.
- `KEYSTORE_PASSWORD`: its password (store and key).

Changing the key changes the store's fingerprint: every extension must then be
reinstalled once on the phone.

## Known limits on Android 6

- Sites whose TLS certificate only chains to Let's Encrypt's ISRG Root X1 fail, because
  Android 6 doesn't trust that root.
- Sites behind a Cloudflare browser check depend on Android 6's old WebView and may fail.

## Licences

The extensions are Keiyoushi's (Apache-2.0), republished converted; `lib/` holds Mihon
classes (Apache-2.0).
