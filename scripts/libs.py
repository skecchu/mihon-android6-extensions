"""Fetch the pinned libraries the converter compiles against.

These are the exact versions Mihon 0.21.3 ships, so the converted extensions are
desugared against the same API the app provides at runtime. Every file is checked
against its SHA-256 before use.

usage: python libs.py <dest-dir> [--gradle-cache <dir>]
"""
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile

CENTRAL = "https://repo1.maven.org/maven2"
GOOGLE = "https://dl.google.com/dl/android/maven2"

# (repo, group, artifact, version, extension, sha256)
LIBS = [
    (CENTRAL, "com.squareup.okio", "okio-jvm", "3.18.2", "jar", "681fed549d1d2c63710f42c9eab433d642c71af778babd67035c50f8c425aaff"),
    (CENTRAL, "org.jetbrains.kotlinx", "kotlinx-serialization-core-jvm", "1.12.0", "jar", "bcfddcac76159058ba0953af05938c17111d238cd41be7191874656454d62f1a"),
    (CENTRAL, "org.jetbrains.kotlinx", "kotlinx-serialization-json-jvm", "1.12.0", "jar", "879eb73e46cb63253ab0fa7cca4448f5c96bca1a16ff377abcb71ebab60e0631"),
    (CENTRAL, "org.jetbrains.kotlinx", "kotlinx-serialization-protobuf-jvm", "1.12.0", "jar", "d68f6aca48c54ae400f0cecc5b359617e630078a2b36a63311c9338eed8846a5"),
    (CENTRAL, "org.jetbrains.kotlinx", "kotlinx-coroutines-core-jvm", "1.11.0", "jar", "d1d75aa01dffbb4d1c520e67e4c4e7f5f6174718e7cb4632412503f2f0e604fa"),
    (CENTRAL, "uy.kohesive.injekt", "injekt-core", "1.16.1", "jar", "dc7ac7533550a88c86519d1554f62083ab56e9ae5e56e0c4f200d41416962217"),
    (CENTRAL, "uy.kohesive.injekt", "injekt-api", "1.16.1", "jar", "ebdc6f061ca873b2440f9eef4d27c809bd66df77232afa4514f7c0ccfb4c78e4"),
    (CENTRAL, "org.jsoup", "jsoup", "1.23.2", "jar", "e9d8c856856680427f096d156f02a414289985c930998fcb519ce5be39b42003"),
    (CENTRAL, "io.reactivex", "rxjava", "1.3.8", "jar", "387df880f226b01cea4b1026d96d34e1da27d5801562742cfce0413c21ef7690"),
    (CENTRAL, "org.jetbrains.kotlin", "kotlin-stdlib", "2.4.21", "jar", "bd8250210584cb659847dce9cda660f8e9906e5def7fbebcea93dc6dcb6a88a3"),
    (CENTRAL, "com.squareup.okhttp3", "okhttp-android", "5.5.1", "aar", "ce00fd7751cf7e4f28ba73a0287b3cc0c62b8b83f038e428592eeef189513ee1"),
    (CENTRAL, "com.squareup.okhttp3", "okhttp-zstd", "5.5.1", "jar", "dea56ed3d4320219d0086cd0ea0e2d240b81c0d8be8a4f0c8683afe722389bca"),
    (GOOGLE, "com.android.tools", "desugar_jdk_libs", "2.1.5", "jar", "d8044befae095781b9a80bf1faa92edc30382d75d437476784c1bf991598a976"),
    (GOOGLE, "com.android.tools", "desugar_jdk_libs_configuration", "2.1.5", "jar", "7bc9051b3a1ec19806311dcb6aa9b9ba7ef9c22caa6f4810da55bde285fb7770"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_LIB = os.path.join(os.path.dirname(HERE), "lib")
# Mihon's own modules (Apache-2.0), built from the v0.21.3 tag; committed in lib/.
MIHON_JARS = ["mihon-source-api-0.21.3.jar", "mihon-core-common-0.21.3.jar"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _from_gradle(cache, group, artifact, version, want):
    base = os.path.join(cache, group, artifact, version)
    if not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        for f in files:
            p = os.path.join(root, f)
            if "sources" not in f and sha256(p) == want:
                return p
    return None


def fetch(dest, gradle_cache=None):
    """Download (or copy) every library into dest. Returns a dict of paths."""
    os.makedirs(dest, exist_ok=True)
    out = {"classpath": []}
    for repo, group, artifact, version, ext, want in LIBS:
        path = os.path.join(dest, "%s-%s.%s" % (artifact, version, ext))
        if not (os.path.exists(path) and sha256(path) == want):
            src = _from_gradle(gradle_cache, group, artifact, version, want) if gradle_cache else None
            if src:
                shutil.copyfile(src, path)
            else:
                url = "%s/%s/%s/%s/%s-%s.%s" % (repo, group.replace(".", "/"), artifact, version, artifact, version, ext)
                urllib.request.urlretrieve(url, path + ".part")
                os.replace(path + ".part", path)
        got = sha256(path)
        if got != want:
            raise SystemExit("checksum mismatch for %s: %s" % (path, got))
        if artifact == "okhttp-android":
            jar = os.path.join(dest, "okhttp-android-%s-classes.jar" % version)
            with zipfile.ZipFile(path) as z, open(jar, "wb") as f:
                f.write(z.read("classes.jar"))
            out["classpath"].append(jar)
        elif artifact == "desugar_jdk_libs":
            out["desugar_lib"] = path
        elif artifact == "desugar_jdk_libs_configuration":
            cfg = os.path.join(dest, "desugar.json")
            with zipfile.ZipFile(path) as z, open(cfg, "wb") as f:
                f.write(z.read("META-INF/desugar/d8/desugar.json"))
            out["desugar_json"] = cfg
        else:
            out["classpath"].append(path)
    for name in MIHON_JARS:
        out["classpath"].append(os.path.join(REPO_LIB, name))
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    cache = None
    if "--gradle-cache" in args:
        i = args.index("--gradle-cache")
        cache = args[i + 1]
        del args[i:i + 2]
    print(fetch(args[0], cache))
