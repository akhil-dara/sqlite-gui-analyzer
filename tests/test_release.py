"""Release tooling: one version source, the version resource, the tag check, zip, checksums, notes."""
import ast
import glob
import hashlib
import os
import re
import subprocess
import sys
import unittest
import zipfile

from tests.helpers import ROOT, TempDirTest

TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import release  # noqa: E402
from constants import VERSION  # noqa: E402

VERSION_LITERAL = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])")
PINNED_TOOL_VERSION = re.compile(r"(?m)(@[0-9a-f]{40} # v\d+\.\d+\.\d+$|choco install \S+ --version=\d+\.\d+\.\d+)")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class VersionSourceTest(unittest.TestCase):
    def test_version_is_x_y_z(self):
        self.assertTrue(release.VERSION_RE.match(VERSION), VERSION)

    def test_release_tool_reads_the_version_constants_py_defines(self):
        self.assertEqual(release.read_version(), VERSION)

    def test_build_files_do_not_hard_code_a_version(self):
        files = ["sqlite-gui-analyzer.spec", os.path.join("installer", "sqlite-gui-analyzer.iss")]
        files += [os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))]
        self.assertGreaterEqual(len(files), 4)
        for name in files:
            # Third-party pins (an action's tag comment, the Inno Setup package) are not the app's
            text = PINNED_TOOL_VERSION.sub("", read(name))
            found = VERSION_LITERAL.findall(text)
            self.assertEqual(found, [], "%s hard-codes a version: %s" % (name, found))

    def test_pyproject_takes_the_version_from_constants(self):
        text = read("pyproject.toml")
        self.assertRegex(text, r'(?m)^dynamic\s*=\s*\[[^\]]*"version"')
        self.assertRegex(text, r'(?m)^version\s*=\s*\{\s*attr\s*=\s*"constants\.VERSION"\s*\}')
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        self.assertNotRegex(project, r'(?m)^version\s*=')

    def test_spec_and_installer_read_the_version(self):
        self.assertIn("release.read_version(SPECPATH)", read("sqlite-gui-analyzer.spec"))
        iss = read("installer", "sqlite-gui-analyzer.iss")
        self.assertIn("#ifndef AppVersion", iss)
        self.assertIn("AppVersion={#AppVersion}", iss)

    def test_no_stale_version_resource_file(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "version.txt")))

    def test_changelog_has_an_entry_for_this_version(self):
        self.assertTrue(release.changelog_section(read("CHANGELOG.md"), VERSION))


class VersionHelpersTest(TempDirTest):
    def test_numeric_version_and_prerelease(self):
        self.assertEqual(release.numeric_version("2.0.0"), (2, 0, 0, 0))
        self.assertEqual(release.numeric_version("10.20.30-rc.1"), (10, 20, 30, 0))
        self.assertFalse(release.is_prerelease("2.0.0"))
        self.assertTrue(release.is_prerelease("2.1.0-beta2"))
        for bad in ("2.0", "v2.0.0", "2.0.0-", "2.0.0 ", "2.0.0-rc_1", ""):
            with self.assertRaises(release.ReleaseError, msg=bad):
                release.numeric_version(bad)

    def test_version_resource_is_a_pyinstaller_version_expression(self):
        text = release.version_resource(VERSION)
        tree = ast.parse(text, mode="exec")
        call = tree.body[0].value
        self.assertEqual(call.func.id, "VSVersionInfo")
        self.assertIn("filevers=%r" % (release.numeric_version(VERSION),), text)
        self.assertIn("StringStruct('ProductVersion', %r)" % VERSION, text)
        self.assertIn("StringStruct('OriginalFilename', 'SQLiteGUIAnalyzer.exe')", text)
        out = release.write_version_resource(os.path.join(self.tmp, "new", "dir", "v.txt"), VERSION)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_read_version_rejects_a_bad_or_missing_version(self):
        os.makedirs(os.path.join(self.tmp, "src"))
        path = os.path.join(self.tmp, "src", "constants.py")
        for body in ('VERSION = "1.0"\n', "VERSION = 2\n", "OTHER = '1.2.3'\n"):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            with self.assertRaises(release.ReleaseError, msg=body):
                release.read_version(self.tmp)
        with open(path, "w", encoding="utf-8") as f:
            f.write('"""doc"""\nX = 1\nVERSION = "3.4.5"\n')
        self.assertEqual(release.read_version(self.tmp), "3.4.5")

    def test_tag_check(self):
        self.assertIsNone(release.check_tag("v" + VERSION, VERSION))
        for tag in (VERSION, "V" + VERSION, "v" + VERSION + ".1", "v" + VERSION + "-rc1", ""):
            self.assertIn("does not match", release.check_tag(tag, VERSION), tag)


class CommandLineTest(unittest.TestCase):
    def run_tool(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, os.path.join(TOOLS, "release.py")] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, env=env, timeout=60)

    def test_version_and_check_tag(self):
        r = self.run_tool("version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, VERSION))
        self.assertEqual(self.run_tool("check-tag", "v" + VERSION).returncode, 0)
        r = self.run_tool("check-tag", "v0.0.1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match VERSION", r.stderr)


class ArtifactTest(TempDirTest):
    def make_tree(self):
        top = os.path.join(self.tmp, "SQLiteGUIAnalyzer")
        os.makedirs(os.path.join(top, "_internal", "sub"))
        files = {"SQLiteGUIAnalyzer.exe": b"MZ exe", "_internal/a.dll": b"dll",
                 "_internal/sub/b.txt": b"text"}
        for rel, data in files.items():
            with open(os.path.join(top, *rel.split("/")), "wb") as f:
                f.write(data)
        return top, files

    def test_zip_puts_everything_under_one_top_folder(self):
        top, files = self.make_tree()
        out = os.path.join(self.tmp, "p.zip")
        self.assertEqual(release.make_zip(top, out, "SQLiteGUIAnalyzer-9.9.9-portable"), 3)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist(), sorted(zf.namelist()))
            got = dict((n, zf.read(n)) for n in zf.namelist())
        self.assertEqual(got, dict(("SQLiteGUIAnalyzer-9.9.9-portable/" + k, v) for k, v in files.items()))
        os.makedirs(os.path.join(self.tmp, "empty"))
        with self.assertRaises(release.ReleaseError):
            release.make_zip(os.path.join(self.tmp, "empty"), os.path.join(self.tmp, "e.zip"))

    def test_checksums_are_sha256sum_lines(self):
        top, files = self.make_tree()
        paths = [os.path.join(top, "SQLiteGUIAnalyzer.exe"), os.path.join(top, "_internal", "a.dll")]
        out = os.path.join(self.tmp, "SHA256SUMS.txt")
        text = release.write_checksums(out, paths)
        want = "%s  SQLiteGUIAnalyzer.exe\n%s  a.dll\n" % (
            hashlib.sha256(b"MZ exe").hexdigest(), hashlib.sha256(b"dll").hexdigest())
        self.assertEqual(text, want)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), want.encode("ascii"))     # LF only: sha256sum -c works

    def test_release_notes(self):
        changelog = ("# Changelog\n\n## [9.9.9] - 2030-01-01\n\n- **New** thing.\n\n"
                     "## 9.9.8\n\n- Old thing.\n")
        self.assertEqual(release.changelog_section(changelog, "9.9.9"), "- **New** thing.")
        self.assertEqual(release.changelog_section(changelog, "9.9.8"), "- Old thing.")
        with self.assertRaises(release.ReleaseError):
            release.changelog_section(changelog, "9.9.7")
        with self.assertRaises(release.ReleaseError):
            release.changelog_section(changelog, "9.9")         # no prefix matches
        notes = release.release_notes("9.9.9", changelog, "abc  SHA-file\n")
        for text in ("What's new in 9.9.9", "- **New** thing.", "SQLiteGUIAnalyzer-9.9.9-setup.exe",
                     "SQLiteGUIAnalyzer-9.9.9-portable.zip", "SQLiteGUIAnalyzer-9.9.9-portable.exe",
                     "SHA256SUMS.txt", "abc  SHA-file"):
            self.assertIn(text, notes)
        self.assertNotIn("Old thing", notes)
        self.assertNotIn("### SHA-256", release.release_notes("9.9.9", changelog))


class PinnedBuildInputsTest(unittest.TestCase):
    """Every action, tool package and build requirement is pinned to exact content."""

    def workflows(self):
        paths = glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
        self.assertGreaterEqual(len(paths), 2)
        return {os.path.basename(p): read(os.path.relpath(p, ROOT)) for p in paths}

    def test_every_action_is_pinned_to_a_commit_sha_with_its_tag(self):
        seen = 0
        for name, text in self.workflows().items():
            for line in text.splitlines():
                if re.match(r"\s*(-\s+)?uses:", line):
                    seen += 1
                    self.assertRegex(line, r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$",
                                     "%s: %s" % (name, line.strip()))
        self.assertGreaterEqual(seen, 4)

    def test_choco_installs_are_pinned(self):
        for name, text in self.workflows().items():
            for line in text.splitlines():
                if "choco install" in line:
                    self.assertRegex(line, r"--version=\d+\.\d+\.\d+", "%s: %s" % (name, line.strip()))

    def test_pip_installs_require_hashes(self):
        installs = 0
        for name, text in self.workflows().items():
            for line in text.splitlines():
                if "pip install" in line:
                    installs += 1
                    self.assertIn("--require-hashes", line, "%s: %s" % (name, line.strip()))
        self.assertGreaterEqual(installs, 1)

    def test_build_requirements_pin_versions_and_hashes(self):
        text = read("tools", "build-requirements.txt").replace("\\\n", " ")
        reqs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        names = set()
        for req in reqs:
            self.assertRegex(req, r"^[A-Za-z0-9._-]+==[^\s=]+(\s+--hash=sha256:[0-9a-f]{64})+$", req)
            names.add(req.split("==")[0].lower())
        self.assertTrue({"pyinstaller", "pillow"} <= names, names)


if __name__ == "__main__":
    unittest.main()
