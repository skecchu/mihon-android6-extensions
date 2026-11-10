"""Publish a build_store.py run: upload new APKs, update the store branch, prune old APKs.

Order matters for phones mid-update: new APKs go up first, then the index that
points at them, and only then are APKs no longer in the index deleted.

usage: python publish.py --out <build_store out dir> --repo owner/name [--branch repo]
Needs the gh CLI with GH_TOKEN, and git.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_README = """# Android 6 extension store

This branch is written by the daily workflow on `main`; don't edit it by hand.

Add this store in Mihon (Settings > Browse > Extension stores):

    https://raw.githubusercontent.com/{repo}/{branch}/index.json

See `report.md` for the last run.
"""


def gh(*args, capture=False):
    r = subprocess.run(["gh"] + list(args), check=True, text=True,
                       stdout=subprocess.PIPE if capture else None)
    return r.stdout if capture else None


def ensure_release(repo, tag):
    if subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    gh("release", "create", tag, "--repo", repo, "--title", "Extension APKs (%s)" % tag,
       "--notes", "Converted extension APKs used by the store index. Updated daily; not meant to be downloaded by hand.",
       "--latest=false")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="repo")
    a = ap.parse_args()

    state = json.load(open(os.path.join(a.out, "state.json"), encoding="utf-8"))
    wanted = {}
    for p in state["extensions"].values():
        wanted.setdefault(p["tag"], set()).add(p["asset"])

    uploads = {}
    with open(os.path.join(a.out, "uploads.tsv"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                tag, path = line.rstrip("\n").split("\t")
                uploads.setdefault(tag, []).append(path)

    for tag in sorted(set(wanted) | set(uploads)):
        ensure_release(a.repo, tag)
    for tag, files in sorted(uploads.items()):
        for i in range(0, len(files), 40):
            gh("release", "upload", tag, "--repo", a.repo, "--clobber", *files[i:i + 40])
        print("uploaded %d to %s" % (len(files), tag), flush=True)

    # Store branch: one fresh commit each run, so its history stays tiny.
    wt = tempfile.mkdtemp()
    for name in ("index.json", "state.json", "report.md"):
        shutil.copy(os.path.join(a.out, name), os.path.join(wt, name))
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write(STORE_README.format(repo=a.repo, branch=a.branch))
    remote = "https://x-access-token:%s@github.com/%s.git" % (os.environ["GH_TOKEN"], a.repo)
    git = ["git", "-C", wt, "-c", "user.name=github-actions[bot]",
           "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
    subprocess.run(git + ["init", "-q", "-b", a.branch], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "Update store"], check=True)
    subprocess.run(git + ["push", "-q", "-f", remote, "HEAD:" + a.branch], check=True)
    print("store branch updated", flush=True)

    # Prune APKs the index no longer points at.
    releases = json.loads(gh("release", "list", "--repo", a.repo, "--limit", "100", "--json", "tagName", capture=True))
    for rel in releases:
        tag = rel["tagName"]
        if not tag.startswith("apks-"):
            continue
        assets = json.loads(gh("release", "view", tag, "--repo", a.repo, "--json", "assets", capture=True))["assets"]
        stale = [x["name"] for x in assets if x["name"] not in wanted.get(tag, set())]
        for name in stale:
            gh("release", "delete-asset", tag, name, "--repo", a.repo, "-y")
        if stale:
            print("pruned %d from %s" % (len(stale), tag), flush=True)


if __name__ == "__main__":
    main()
