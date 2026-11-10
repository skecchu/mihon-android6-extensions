"""Build the Android 6 extension store from Keiyoushi's current index.

Only extensions whose Keiyoushi version changed since the last run (per the
previous state.json) are converted; the rest are carried over. An extension
that fails to convert keeps its previous converted version if there is one.

Environment:
  A6_KEYSTORE, A6_KS_PASS, A6_KS_ALIAS   signing key
  JAVA_HOME                              JDK 17+
  ANDROID_BUILD_TOOLS                    build-tools dir (d8.jar, apksigner.jar, zipalign)
  ANDROID_JAR                            platforms/android-34/android.jar

Outputs in --out:
  index.json   the Mihon store index
  state.json   what was published (input to the next run)
  report.md    counts and failures
  uploads.tsv  "<release tag>\t<file>" for every new APK to upload
  apk/         the new APKs
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import a6conv  # noqa: E402
import libs  # noqa: E402
import storepb  # noqa: E402

KEIYOUSHI_INDEX = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.json"
WARNINGS = {
    "CONTENT_WARNING_SAFE": "SAFE", "CONTENT_WARNING_MIXED": "MIXED", "CONTENT_WARNING_NSFW": "NSFW",
    "SAFE": "SAFE", "MIXED": "MIXED", "NSFW": "NSFW",
}


def log(*a):
    print(*a, flush=True)


def fetch(url, path=None, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "a6-extension-converter"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if path:
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
            return data
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def _jar_count(idx):
    """Extensions with a .jar link, or -1 if this isn't a store index at all."""
    try:
        return sum(1 for e in idx["extensionList"]["extensions"] if e["resources"].get("jarUrl"))
    except (KeyError, TypeError, AttributeError):
        return -1


def load_keiyoushi(src):
    """Read Keiyoushi's index.json; fall back to the index.pb beside it if the JSON is
    missing, unreadable or has no .jar links. Returns (index, the file it came from)."""
    def read(s):
        data = fetch(s) if s.startswith("http") else open(s, "rb").read()
        return storepb.parse(data) if s.endswith(".pb") else json.loads(data)

    try:
        idx = read(src)
        if _jar_count(idx) > 0:
            return idx, src
        problem = "no extensions with a .jar link"
    except Exception as ex:
        problem = "%s: %s" % (type(ex).__name__, ex)
    if not src.endswith(".json"):
        raise RuntimeError("Keiyoushi index %s unusable (%s)" % (src, problem))
    pb = src[:-len(".json")] + ".pb"
    log("index.json unusable (%s); trying %s" % (problem, pb))
    idx = read(pb)
    if _jar_count(idx) <= 0:
        raise RuntimeError("neither %s (%s) nor %s has extensions with a .jar link" % (src, problem, pb))
    return idx, pb


def shard_tag(pkg, shards):
    n = int(hashlib.sha1(pkg.encode()).hexdigest(), 16) % shards
    return "apks-%d" % n


def signing_fingerprint(java_home, keystore, alias):
    keytool = os.path.join(java_home, "bin", "keytool")
    der = subprocess.run([keytool, "-exportcert", "-keystore", keystore, "-alias", alias,
                          "-storepass:env", "A6_KS_PASS"], check=True, stdout=subprocess.PIPE).stdout
    return hashlib.sha256(der).hexdigest()


def index_entry(ext, apk_url, version_code):
    return {
        "name": ext["name"],
        "packageName": ext["packageName"],
        "resources": {"apkUrl": apk_url, "iconUrl": ext["resources"]["iconUrl"]},
        "extensionLib": ext["extensionLib"],
        "versionCode": version_code,
        "versionName": ext["versionName"],
        "contentWarning": WARNINGS.get(ext.get("contentWarning", ""), "SAFE"),
        "sources": [
            {k: v for k, v in {
                "id": int(s["id"]),
                "name": s["name"],
                "language": s["language"],
                "homeUrl": s.get("homeUrl", ""),
                "mirrorUrls": s.get("mirrorUrls") or None,
                "message": s.get("message"),
            }.items() if v is not None}
            for s in ext["sources"]
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name of this GitHub repo")
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--keiyoushi", default=KEIYOUSHI_INDEX, help="URL or path of Keiyoushi's index.json")
    ap.add_argument("--prev-state", help="state.json from the previous run")
    ap.add_argument("--shards", type=int, default=4, help="releases to spread APKs over (1000 assets each)")
    ap.add_argument("--only", help="comma-separated package names (testing)")
    ap.add_argument("--limit", type=int, help="convert at most this many (testing)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--gradle-cache", help="take libraries from this Gradle cache instead of downloading")
    a = ap.parse_args()

    java_home = os.environ["JAVA_HOME"]
    java = os.path.join(java_home, "bin", "java")
    keystore = os.environ["A6_KEYSTORE"]
    ks_pass = os.environ["A6_KS_PASS"]
    alias = os.environ.get("A6_KS_ALIAS", "ext")

    os.makedirs(os.path.join(a.out, "apk"), exist_ok=True)
    os.makedirs(a.work, exist_ok=True)

    log("libraries ...")
    lib_paths = libs.fetch(os.path.join(a.work, "libs"), a.gradle_cache)
    log("toolchain (companion classes) ...")
    tc = a6conv.Toolchain(java, os.environ["ANDROID_BUILD_TOOLS"], os.environ["ANDROID_JAR"], lib_paths,
                          os.path.join(a.work, "tc"), keystore, ks_pass, alias)
    fingerprint = signing_fingerprint(java_home, keystore, alias)

    kidx, kidx_from = load_keiyoushi(a.keiyoushi)
    exts = kidx["extensionList"]["extensions"]
    if a.only:
        wanted = set(a.only.split(","))
        exts = [e for e in exts if e["packageName"] in wanted]
    log("keiyoushi extensions:", len(exts))

    prev = {}
    if a.prev_state and os.path.exists(a.prev_state):
        st = json.load(open(a.prev_state, encoding="utf-8"))
        if st.get("signingKey") == fingerprint:
            prev = st.get("extensions", {})
        else:
            log("signing key changed: converting everything again")

    todo, carried = [], {}
    for e in exts:
        pkg = e["packageName"]
        p = prev.get(pkg)
        if p and p["keiyoushiVersionCode"] == int(e["versionCode"]) and p["revision"] == a6conv.CONVERTER_REVISION:
            carried[pkg] = p
        else:
            todo.append(e)
    if a.limit is not None:
        todo = todo[:a.limit]
    log("to convert:", len(todo), "carried over:", len(carried))

    dl_dir = os.path.join(a.work, "dl")
    os.makedirs(dl_dir, exist_ok=True)

    def job(e):
        pkg = e["packageName"]
        res = e["resources"]
        if not res.get("jarUrl"):
            raise a6conv.ConvertError("Keiyoushi publishes no .jar for it")
        asset = res["apkUrl"].rsplit("/", 1)[-1]
        apk_in = os.path.join(dl_dir, asset)
        jar_in = apk_in[:-4] + ".jar"
        fetch(res["apkUrl"], apk_in)
        fetch(res["jarUrl"], jar_in)
        tag = shard_tag(pkg, a.shards)
        out_dir = os.path.join(a.out, "apk", tag)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, asset)
        code = a6conv.converted_version_code(e["versionCode"])
        t = time.time()
        tc.convert(apk_in, jar_in, out, code)
        for f in (apk_in, jar_in):
            os.remove(f)
        url = "https://github.com/%s/releases/download/%s/%s" % (a.repo, tag, asset)
        return {
            "keiyoushiVersionCode": int(e["versionCode"]),
            "revision": a6conv.CONVERTER_REVISION,
            "tag": tag,
            "asset": asset,
            "size": os.path.getsize(out),
            "entry": index_entry(e, url, code),
            "seconds": round(time.time() - t, 1),
        }

    done, failed, uploads = {}, {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futs = {pool.submit(job, e): e for e in todo}
        for i, f in enumerate(concurrent.futures.as_completed(futs), 1):
            e = futs[f]
            pkg = e["packageName"]
            try:
                r = f.result()
                done[pkg] = r
                uploads.append((r["tag"], os.path.join(a.out, "apk", r["tag"], r["asset"])))
                log("[%d/%d] ok   %s %s (%d KB, %ss)" % (i, len(todo), pkg, e["versionName"], r["size"] // 1024, r["seconds"]))
            except Exception as ex:
                msg = str(ex) if isinstance(ex, a6conv.ConvertError) else traceback.format_exc()
                failed[pkg] = msg.strip().splitlines()[-1][:300] if msg.strip() else repr(ex)
                log("[%d/%d] FAIL %s: %s" % (i, len(todo), pkg, failed[pkg]))

    published = dict(carried)
    published.update(done)
    kept_old = []
    for pkg in failed:
        if pkg in prev and pkg not in published:
            published[pkg] = prev[pkg]
            kept_old.append(pkg)
    # Extensions not attempted this run (--limit) stay as they were.
    attempted = {e["packageName"] for e in todo}
    for e in exts:
        pkg = e["packageName"]
        if pkg not in published and pkg in prev and pkg not in attempted:
            published[pkg] = prev[pkg]

    order = {e["packageName"]: i for i, e in enumerate(exts)}
    entries = [published[p]["entry"] for p in sorted(published, key=lambda p: order.get(p, 1 << 30))]
    index = {
        "name": "Android 6 extensions",
        "badgeLabel": "A6",
        "signingKey": fingerprint,
        "contact": {"website": "https://github.com/%s" % a.repo, "discord": None},
        "extensionList": {"extensions": entries},
        "extensionListUrl": None,
    }
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(a.out, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"signingKey": fingerprint, "extensions": published}, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(a.out, "uploads.tsv"), "w", encoding="utf-8") as f:
        for tag, path in uploads:
            f.write("%s\t%s\n" % (tag, path))

    never = sorted(p for p in failed if p not in published)
    lines = [
        "# Last run",
        "",
        "- Keiyoushi extensions: %d (from `%s`)" % (len(exts), kidx_from.rsplit("/", 1)[-1]),
        "- Published: %d" % len(published),
        "- Converted this run: %d" % len(done),
        "- Failed this run: %d (%d kept their previous version)" % (len(failed), len(kept_old)),
        "",
    ]
    if failed:
        lines += ["## Failures", ""] + ["- `%s`%s: %s" % (p, " (previous version kept)" if p in kept_old else "", failed[p])
                                         for p in sorted(failed)] + [""]
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log("published %d, converted %d, failed %d (never published: %d)" % (len(published), len(done), len(failed), len(never)))


if __name__ == "__main__":
    main()
