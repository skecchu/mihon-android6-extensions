"""Convert one Keiyoushi extension so it installs and runs on Android 6 (API 23).

Keiyoushi APKs need Android 8: minSdk 26, dex version 038, v2-only signature.
Starting from the extension's published .jar (plain class files) we:

1. dex it with D8 for API 23, with core library desugaring, against the exact
   library versions Mihon provides at runtime;
2. bundle a trimmed desugared Java library (j$.*) holding only what the
   extension calls (built with L8, cached by the set of kept members);
3. bundle the interface companion classes ($-CC) of Mihon's libraries, which
   D8 calls into for default/static interface methods but Mihon's R8 removed;
4. lower minSdkVersion to 23 and bump versionCode in the binary manifest;
5. zipalign and sign with v1+v2+v3 signatures.

This works because Mihon loads extensions child-first
(DelegateLastClassLoader), so the bundled j$ and $-CC classes win.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile

import axml
import dexrefs

MIN_API = 23
# Bump when a converter change must reach phones: it is added to versionCode,
# so installed extensions see an update even if Keiyoushi's version is the same.
CONVERTER_REVISION = 1


def converted_version_code(keiyoushi_code):
    return int(keiyoushi_code) * 100 + CONVERTER_REVISION


class ConvertError(Exception):
    pass


def _run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        tool = next((a for a in cmd if a.startswith("com.android.tools.r8.")), None) \
            or next((a for a in cmd if a.endswith(".jar")), cmd[0])
        raise ConvertError("%s failed:\n%s" % (os.path.basename(tool), p.stdout[-4000:]))
    return p.stdout


class Toolchain:
    def __init__(self, java, build_tools, android_jar, libs, work, keystore, ks_pass, ks_alias):
        self.java = java
        self.d8jar = os.path.join(build_tools, "lib", "d8.jar")
        self.apksigner = os.path.join(build_tools, "lib", "apksigner.jar")
        za = os.path.join(build_tools, "zipalign")
        self.zipalign = za + ".exe" if os.path.exists(za + ".exe") else za
        self.android_jar = android_jar
        self.classpath = libs["classpath"]
        self.desugar_json = libs["desugar_json"]
        self.desugar_lib = libs["desugar_lib"]
        self.work = work
        self.keystore = keystore
        os.environ["A6_KS_PASS"] = ks_pass
        self.ks_alias = ks_alias
        self._l8_locks = {}
        self._l8_guard = threading.Lock()
        os.makedirs(work, exist_ok=True)
        self.companions_dex = self._build_companions()
        self.companion_refs = dexrefs.refs(self.companions_dex)

    # -- tools ---------------------------------------------------------------
    def _r8(self, main, *args):
        return _run([self.java, "-Xmx2g", "-cp", self.d8jar, "com.android.tools.r8." + main] + list(args))

    def _d8_common(self):
        args = ["--release", "--min-api", str(MIN_API), "--lib", self.android_jar,
                "--desugared-lib", self.desugar_json]
        return args

    def _cp_args(self):
        out = []
        for c in self.classpath:
            out += ["--classpath", c]
        return out

    # -- shared pieces -------------------------------------------------------
    def _build_companions(self):
        """Dex the $-CC companion classes of every runtime library, once."""
        out_dir = os.path.join(self.work, "companions")
        dex = os.path.join(out_dir, "classes.dex")
        if os.path.exists(dex):
            return dex
        tmp = tempfile.mkdtemp(dir=self.work)
        cf = os.path.join(tmp, "libs-cf.jar")
        self._r8("D8", "--classfile", *self._d8_common(), "--output", cf, *self.classpath)
        comp = os.path.join(tmp, "companions.jar")
        n = 0
        with zipfile.ZipFile(cf) as zin, zipfile.ZipFile(comp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in zin.namelist():
                if name.endswith("$-CC.class"):
                    zout.writestr(name, zin.read(name))
                    n += 1
        if n == 0:
            raise ConvertError("no $-CC companion classes found")
        os.makedirs(out_dir, exist_ok=True)
        self._r8("D8", *self._d8_common(), *self._cp_args(), "--output", out_dir, comp)
        shutil.rmtree(tmp, ignore_errors=True)
        return dex

    def _l8_for(self, refs_list):
        """Build (or reuse) a trimmed j$ dex keeping the referenced members."""
        keep = {}
        for types, members in refs_list:
            for t in types:
                c = dexrefs.desc_to_class(t)
                if c and c.startswith("j$."):
                    keep.setdefault(c, set())
            for cls, kind, name in members:
                c = dexrefs.desc_to_class(cls)
                if c and c.startswith("j$."):
                    keep.setdefault(c, set()).add((kind, name))
        if not keep:
            return None
        lines = ["-dontobfuscate", "-dontoptimize", "-dontwarn **"]
        for c in sorted(keep):
            body = []
            for kind, name in sorted(keep[c]):
                if kind == "m":
                    body.append(("%s(...);" % name) if name in ("<init>", "<clinit>") else ("*** %s(...);" % name))
                else:
                    body.append("*** %s;" % name)
            lines.append("-keep class %s { %s }" % (c, " ".join(body)) if body else "-keep class %s" % c)
        rules = "\n".join(lines) + "\n"
        key = hashlib.sha256(rules.encode()).hexdigest()[:16]
        out_dir = os.path.join(self.work, "l8", key)
        dex = os.path.join(out_dir, "classes.dex")
        with self._l8_guard:
            lock = self._l8_locks.setdefault(key, threading.Lock())
        with lock:
            if not os.path.exists(dex):
                tmp = out_dir + ".tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                os.makedirs(tmp)
                pro = os.path.join(tmp, "keep.pro")
                open(pro, "w").write(rules)
                self._r8("L8", "--release", "--min-api", str(MIN_API), "--desugared-lib", self.desugar_json,
                         "--lib", self.android_jar, "--pg-conf", pro, "--output", tmp, self.desugar_lib)
                shutil.rmtree(out_dir, ignore_errors=True)
                os.replace(tmp, out_dir)
        return dex

    # -- one extension -------------------------------------------------------
    def convert(self, apk_in, jar_in, apk_out, version_code):
        tmp = tempfile.mkdtemp(dir=self.work)
        try:
            dex_dir = os.path.join(tmp, "dex")
            os.makedirs(dex_dir)
            self._r8("D8", *self._d8_common(), *self._cp_args(), "--output", dex_dir, jar_in)
            ext_dex = sorted((f for f in os.listdir(dex_dir) if f.endswith(".dex")),
                             key=lambda f: (len(f), f))
            if not ext_dex:
                raise ConvertError("D8 produced no dex")
            dexes = [os.path.join(dex_dir, f) for f in ext_dex]
            l8 = self._l8_for([dexrefs.refs(d) for d in dexes] + [self.companion_refs])
            if l8:
                dexes.append(l8)
            dexes.append(self.companions_dex)

            with zipfile.ZipFile(apk_in) as z:
                manifest = bytearray(z.read("AndroidManifest.xml"))
            axml.set_int(manifest, axml.MIN_SDK_VERSION, MIN_API)
            axml.set_int(manifest, axml.VERSION_CODE, version_code)

            unsigned = os.path.join(tmp, "unsigned.apk")
            _repack(apk_in, unsigned, bytes(manifest), dexes)
            aligned = os.path.join(tmp, "aligned.apk")
            _run([self.zipalign, "-f", "-p", "4", unsigned, aligned])
            env_pass = "env:A6_KS_PASS"
            _run([self.java, "-jar", self.apksigner, "sign", "--ks", self.keystore, "--ks-pass", env_pass,
                  "--ks-key-alias", self.ks_alias, "--key-pass", env_pass, "--min-sdk-version", str(MIN_API),
                  "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
                  "--out", apk_out, aligned])
            idsig = apk_out + ".idsig"
            if os.path.exists(idsig):
                os.remove(idsig)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _is_sig(name):
    if not name.startswith("META-INF/"):
        return False
    base = name.rsplit("/", 1)[-1].upper()
    return base == "MANIFEST.MF" or base.endswith((".SF", ".RSA", ".DSA", ".EC"))


def _is_dex(name):
    return "/" not in name and name.startswith("classes") and name.endswith(".dex")


def _repack(src, dst, manifest, dexes):
    """Copy src to dst: new manifest, our dex files, no old signatures or dex."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            name = info.filename
            if _is_sig(name) or _is_dex(name):
                continue
            data = manifest if name == "AndroidManifest.xml" else zin.read(info)
            zi = zipfile.ZipInfo(name, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
        for i, path in enumerate(dexes):
            name = "classes.dex" if i == 0 else "classes%d.dex" % (i + 1)
            zout.write(path, name, compress_type=zipfile.ZIP_DEFLATED)
