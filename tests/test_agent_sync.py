"""agent-sync tests: pure helpers, plus end-to-end runs that simulate several machines.

Each simulated machine is a temp HOME; a bare git repo stands in for GitHub. Run with:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "agent-sync"
sys.path.insert(0, str(ROOT))

from agent_sync import claudemd, jsonutil, scan, timer  # noqa: E402

ID = ["--git-name", "Test User", "--git-email", "test@example.com"]
OLD = time.time() - 3600


def age(path: Path) -> None:
    """Backdate a file or tree so adoption's quiet period has passed."""
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (OLD, OLD))
    os.utime(path, (OLD, OLD))


class Machine:
    def __init__(self, root: Path, name: str):
        self.name = name
        self.home = root / name
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "GIT_"))}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")

    @property
    def repo(self) -> Path:
        return self.home / ".local/share/agent-config"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local/state/agent-sync"

    def run(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, str(ENTRY), *args], env=self.env, capture_output=True, text=True,
                           timeout=120)
        if ok and r.returncode != 0:
            raise AssertionError(f"{self.name}: agent-sync {' '.join(args)} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def git(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        env = dict(self.env, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="T",
                   GIT_COMMITTER_EMAIL="t@example.com", GIT_EDITOR="true")
        r = subprocess.run(["git", "-C", str(self.repo), *args], env=env, capture_output=True, text=True)
        if ok and r.returncode != 0:
            raise AssertionError(f"git {args} failed: {r.stderr}")
        return r

    def write(self, rel: str, text: str, old: bool = False) -> Path:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if old:
            age(p.parent if p.parent.parent.name in ("skills",) else p)
        return p

    def read(self, rel: str) -> str:
        return (self.home / rel).read_text()

    def settings(self) -> dict:
        return json.loads((self.claude / "settings.json").read_text())

    def set_settings(self, data: dict) -> None:
        (self.claude / "settings.json").write_text(json.dumps(data, indent=2))

    def hostfile(self) -> dict:
        return json.loads((self.repo / "hosts" / f"{self.name}.json").read_text())

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text())

    def hook(self) -> str:
        return self.run("hook", "session-start").stdout


def without_hook(settings: dict) -> dict:
    from agent_sync.settings import strip_tool_hook

    return strip_tool_hook(settings)


# ---- pure helpers -------------------------------------------------------------------------------


class JsonUtilTest(unittest.TestCase):
    CASES = [
        ({}, {"a": 1}),
        ({"a": 1, "b": {"c": 2, "d": 3}}, {"a": 1, "b": {"c": 5}}),
        ({"a": {"x": 1}}, {"a": "now a string"}),
        ({"a": "string"}, {"a": {"now": "dict"}}),
        ({"hooks": {"Stop": [1, 2]}}, {"hooks": {"Stop": [2]}}),
        ({"a": True}, {"a": 1}),
        ({"a": {"b": {"c": 1}}}, {}),
        ({"a": {}}, {"a": {"b": 1}}),
    ]

    def test_diff_then_merge_round_trips(self):
        for base, desired in self.CASES:
            with self.subTest(base=base, desired=desired):
                overlay = jsonutil.diff(base, desired)
                self.assertTrue(jsonutil.same(jsonutil.merge(base, overlay), desired), overlay)

    def test_diff_is_minimal(self):
        self.assertEqual(jsonutil.diff({"a": 1, "b": 2}, {"a": 1, "b": 3}), {"b": 3})
        self.assertEqual(jsonutil.diff({"a": 1}, {"a": 1}), {})

    def test_true_and_one_are_different(self):
        self.assertEqual(jsonutil.diff({"a": True}, {"a": 1}), {"a": 1})

    def test_common(self):
        hosts = [{"a": 1, "env": {"X": "1", "Y": "2"}, "m": "p"}, {"a": 1, "env": {"X": "1"}, "m": "q"}]
        self.assertEqual(jsonutil.common(hosts), {"a": 1, "env": {"X": "1"}})

    def test_unsubstitute_respects_path_boundaries(self):
        mapping = [("{{HOME}}", "/home/me")]
        self.assertEqual(jsonutil.unsubstitute("/home/me/bin/x", mapping), "{{HOME}}/bin/x")
        self.assertEqual(jsonutil.unsubstitute("/home/meg/bin/x", mapping), "/home/meg/bin/x")
        self.assertEqual(jsonutil.unsubstitute("python3 /home/me", mapping), "python3 {{HOME}}")
        self.assertEqual(jsonutil.substitute("{{HOME}}/x", mapping), "/home/me/x")

    def test_longest_placeholder_wins(self):
        mapping = [("{{CONFIG}}", "/home/me/.local/share/agent-config"), ("{{HOME}}", "/home/me")]
        self.assertEqual(jsonutil.unsubstitute("/home/me/.local/share/agent-config/hooks/x.sh", mapping),
                         "{{CONFIG}}/hooks/x.sh")


class ScanTest(unittest.TestCase):
    def test_finds_known_shapes(self):
        self.assertIn("GitHub token", scan.find("token: ghp_" + "a1B2" * 9))
        self.assertIn("Anthropic API key", scan.find("sk-ant-" + "x" * 30))
        self.assertIn("private key", scan.find("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertIn("assigned secret", scan.find("MY_API_KEY=abc123def456ghi789jkl012"))  # gitleaks:allow

    def test_ignores_ordinary_text(self):
        self.assertEqual(scan.find('"max_tokens": 1000000000000000000000'), [])
        self.assertEqual(scan.find("token: $GITHUB_TOKEN"), [])
        self.assertEqual(scan.find("Secrets management: None configured"), [])
        self.assertEqual(scan.find('"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h"'), [])

    def test_allow_pragma(self):
        self.assertEqual(scan.find("ghp_" + "a1B2" * 9 + "  # agent-sync: allow"), [])

    def test_env_names(self):
        self.assertTrue(scan.is_secret_setting(["env", "GITHUB_TOKEN"], "short"))
        self.assertFalse(scan.is_secret_setting(["env", "GIT_AUTHOR_NAME"], "Someone"))
        self.assertFalse(scan.is_secret_setting(["apiKeyHelper"], "/usr/local/bin/key-helper"))


class ClaudeMdTest(unittest.TestCase):
    DOCKET = "<!-- BEGIN docket (managed) -->\ndocket text\n<!-- END docket -->\n"

    def test_upsert_keeps_other_blocks_and_is_idempotent(self):
        blk = claudemd.BEGIN + "\n@x\n" + claudemd.END
        once = claudemd.upsert(self.DOCKET, blk)
        self.assertIn("docket text", once)
        self.assertEqual(claudemd.upsert(once, blk), once)
        self.assertEqual(claudemd.outside_text(once), "")

    def test_outside_text_and_remove(self):
        doc = "my rule\n\n" + self.DOCKET
        self.assertEqual(claudemd.outside_text(doc), "my rule")
        blk = claudemd.BEGIN + "\n@x\n" + claudemd.END
        self.assertEqual(claudemd.remove(claudemd.upsert(doc, blk)).strip(), doc.strip())


class TimerTest(unittest.TestCase):
    def test_launchd_plist(self):
        text = timer.launchd_plist("/opt/agent-sync", "/usr/bin:/bin", Path("/tmp/state"))
        self.assertIn("<integer>300</integer>", text)
        self.assertIn("<string>/opt/agent-sync</string>", text)
        self.assertIn("<string>--quiet</string>", text)

    def test_systemd_units(self):
        service, unit = timer.systemd_units("/opt/agent-sync", "/usr/bin:/bin")
        self.assertIn('ExecStart="/opt/agent-sync" --quiet', service)
        self.assertIn("OnUnitActiveSec=5min", unit)
        self.assertIn("Type=oneshot", service)


# ---- end to end ---------------------------------------------------------------------------------


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-test-"))
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.url = str(self.remote)
        self.a = Machine(self.tmp, "a")
        self.b = Machine(self.tmp, "b")
        a = self.a
        a.write(".claude/skills/foo/SKILL.md", "---\nname: foo\n---\nv1\n", old=True)
        a.write(".claude/commands/hello.md", "say hello\n")
        a.set_settings({"model": "a-model", "env": {"X": "1"}, "statusLine": {"command": f"{a.home}/bin/status"}})
        a.write(".claude/CLAUDE.md", "A-only rule\n")
        a.run("new", "--host", "a", "--remote", self.url, *ID)
        self.b.set_settings({"model": "b-model"})
        self.b.run("init", self.url, "--host", "b", "--adopt", *ID)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def remote_files(self) -> str:
        return subprocess.run(["git", "-C", str(self.remote), "ls-tree", "-r", "--name-only", "main"],
                              capture_output=True, text=True).stdout

    def outsider(self) -> Machine:
        """A plain clone of the remote, standing in for an edit made outside agent-sync (e.g. on GitHub)."""
        o = Machine(self.tmp, "outsider")
        subprocess.run(["git", "clone", "-q", self.url, str(o.repo)], check=True, env=o.env)
        return o

    def push_as_outsider(self, o: Machine, message: str, force: bool = False) -> None:
        o.git("add", "-A")
        o.git("commit", "-q", "-m", message)
        o.git("push", "-q", *(["--force"] if force else []), "origin", "HEAD:main")


class TwoMachines(Base):
    # setup ------------------------------------------------------------------------------------

    def test_setup_links_and_preserves_each_machines_settings(self):
        a, b = self.a, self.b
        for m in (a, b):
            link = m.claude / "skills" / "foo"
            self.assertTrue(link.is_symlink(), m.name)
            self.assertEqual(os.path.realpath(link), os.path.realpath(m.repo / "skills" / "foo"))
            self.assertTrue((m.claude / "commands" / "hello.md").is_symlink())
        self.assertEqual(without_hook(a.settings()),
                         {"model": "a-model", "env": {"X": "1"}, "statusLine": {"command": f"{a.home}/bin/status"}})
        self.assertEqual(without_hook(b.settings()), {"model": "b-model"})
        # paths are stored with placeholders, so the overlay is portable
        self.assertEqual(a.hostfile()["settings"]["statusLine"]["command"], "{{HOME}}/bin/status")
        self.assertIn("hook session-start", json.dumps(a.settings()))
        self.assertEqual(a.hook(), "")
        self.assertEqual(b.hook(), "")

    def test_claude_md_text_moves_to_host_file(self):
        doc = self.a.read(".claude/CLAUDE.md")
        self.assertIn(claudemd.BEGIN_PREFIX, doc)
        self.assertNotIn("A-only rule", doc)
        self.assertIn("A-only rule", (self.a.repo / "hosts" / "a.md").read_text())
        self.assertIn("hosts/a.md", doc)
        self.assertNotIn("hosts/b.md", self.b.read(".claude/CLAUDE.md"))

    # propagation ------------------------------------------------------------------------------

    def test_skill_edit_reaches_the_other_machine(self):
        self.a.write(".claude/skills/foo/SKILL.md", "---\nname: foo\n---\nv2\n")
        self.a.run()
        self.b.run()
        self.assertIn("v2", self.b.read(".claude/skills/foo/SKILL.md"))
        log = self.a.git("log", "-1", "--format=%s %ae").stdout
        self.assertIn("a: skills/foo", log)
        self.assertIn("test@example.com", log)

    def test_new_skill_is_adopted_after_its_quiet_period(self):
        self.a.write(".claude/skills/bar/SKILL.md", "bar\n", old=True)
        self.a.write(".claude/skills/fresh/SKILL.md", "still being written\n")
        self.a.run()
        self.assertTrue((self.a.claude / "skills/bar").is_symlink())
        self.assertFalse((self.a.claude / "skills/fresh").is_symlink())
        self.b.run()
        self.assertEqual(self.b.read(".claude/skills/bar/SKILL.md"), "bar\n")
        self.assertFalse((self.b.claude / "skills/fresh").exists())

    def test_claude_ai_synced_and_foreign_links_are_left_alone(self):
        b = self.b
        b.write(".claude/skills/synced/SKILL.md", "claude.ai's\n", old=True)
        b.write(".claude/skills/synced/x/SKILL.md", "claude.ai's\n", old=True)
        age(b.claude / "skills/synced")
        elsewhere = self.tmp / "elsewhere" / "other"
        elsewhere.mkdir(parents=True)
        (elsewhere / "SKILL.md").write_text("another tool's skill\n")
        os.symlink(elsewhere, b.claude / "skills" / "other")
        b.run()
        self.assertFalse((b.claude / "skills/synced").is_symlink())
        self.assertEqual(os.readlink(b.claude / "skills/other"), str(elsewhere))
        self.assertNotIn("synced", self.remote_files())
        self.assertNotIn("other", self.remote_files())

    def test_only_and_exclude_limit_where_entries_go(self):
        manifest = json.loads((self.a.repo / "agent-sync.json").read_text())
        manifest["only"] = {"skills/foo": ["a"]}
        (self.a.repo / "agent-sync.json").write_text(json.dumps(manifest))
        self.a.run()
        self.b.run()
        self.assertFalse((self.b.claude / "skills/foo").exists())
        self.assertTrue((self.a.claude / "skills/foo").is_symlink())
        hostfile = self.b.hostfile()
        hostfile["exclude"] = {"commands": ["hello.md"]}
        (self.b.repo / "hosts/b.json").write_text(json.dumps(hostfile))
        self.b.run()
        self.assertFalse((self.b.claude / "commands/hello.md").exists())

    # settings ---------------------------------------------------------------------------------

    def test_local_settings_change_stays_on_that_machine(self):
        s = self.a.settings()
        s["model"] = "a2"
        self.a.set_settings(s)
        self.a.run()
        self.assertEqual(self.a.hostfile()["settings"]["model"], "a2")
        self.assertNotIn("model", json.loads((self.a.repo / "settings/base.json").read_text()))
        self.b.run()
        self.assertEqual(self.b.settings()["model"], "b-model")
        self.assertEqual(self.a.settings()["model"], "a2")

    def test_base_change_reaches_everyone_without_being_pinned_by_local_edits(self):
        (self.a.repo / "settings/base.json").write_text(json.dumps({"effortLevel": "high"}))
        self.a.run()
        s = self.b.settings()
        s["model"] = "b2"  # B changes something else before it has pulled
        self.b.set_settings(s)
        self.b.run()
        self.assertEqual(self.b.settings()["effortLevel"], "high")
        self.assertEqual(self.b.settings()["model"], "b2")
        self.assertNotIn("effortLevel", self.b.hostfile().get("settings", {}))
        self.a.run()
        self.assertEqual(self.a.settings()["effortLevel"], "high")

    def test_secret_settings_stay_local(self):
        s = self.a.settings()
        s["env"]["MY_SERVICE_TOKEN"] = "abc"
        self.a.set_settings(s)
        self.a.run()
        self.assertNotIn("MY_SERVICE_TOKEN", json.dumps(self.a.hostfile()))
        self.assertEqual(self.a.settings()["env"]["MY_SERVICE_TOKEN"], "abc")
        self.assertNotIn("MY_SERVICE_TOKEN", subprocess.run(
            ["git", "-C", str(self.remote), "log", "-p", "main"], capture_output=True, text=True).stdout)

    def test_unparseable_settings_are_left_alone(self):
        (self.a.claude / "settings.json").write_text("{ half written")
        self.a.run()
        self.assertEqual((self.a.claude / "settings.json").read_text(), "{ half written")
        self.assertIn("settings", self.a.state()["problems"])
        self.assertIn("isn't valid JSON", self.a.hook())

    def test_factor_moves_shared_settings_to_base_without_changing_any_machine(self):
        for m in (self.a, self.b):
            s = m.settings()
            s["theme"] = "dark"
            m.set_settings(s)
            m.run()
        self.a.run()
        before = {m.name: m.settings() for m in (self.a, self.b)}
        self.a.run("settings", "factor")
        self.a.run()
        self.b.run()
        self.assertEqual(json.loads((self.a.repo / "settings/base.json").read_text()).get("theme"), "dark")
        self.assertNotIn("theme", self.b.hostfile().get("settings", {}))
        for m in (self.a, self.b):
            self.assertEqual(m.settings(), before[m.name])

    # safety -----------------------------------------------------------------------------------

    def test_conflict_pauses_sync_and_the_hook_says_how_to_fix_it(self):
        self.a.write(".claude/skills/foo/SKILL.md", "A's version\n")
        self.b.write(".claude/skills/foo/SKILL.md", "B's version\n")
        self.a.run()
        self.b.run()
        self.assertIn("conflict", self.b.state()["problems"])
        hook = self.b.hook()
        self.assertIn("skills/foo/SKILL.md", hook)
        self.assertIn("git pull --rebase", hook)
        self.assertIn("B's version", self.b.git("log", "-p", "-1").stdout)  # local commit kept
        # resolve the way the hook says
        self.b.git("pull", "--rebase", ok=False)
        (self.b.repo / "skills/foo/SKILL.md").write_text("merged\n")
        self.b.git("add", "-A")
        self.b.git("-c", "core.editor=true", "rebase", "--continue")
        self.b.run()
        self.assertEqual(self.b.hook(), "")
        self.a.run()
        self.assertEqual(self.a.read(".claude/skills/foo/SKILL.md"), "merged\n")

    def test_secret_in_a_skill_blocks_the_commit(self):
        self.a.write(".claude/skills/foo/notes.txt", "GITHUB_TOKEN=ghp_" + "a1B2" * 9 + "\n")
        self.a.run()
        self.assertIn("held", self.a.state()["problems"])
        self.assertIn("possible secrets", self.a.hook())
        self.assertNotIn("notes.txt", self.remote_files())
        (self.a.claude / "skills/foo/notes.txt").unlink()
        self.a.run()
        self.assertEqual(self.a.hook(), "")

    def test_mass_deletion_is_held_until_allowed(self):
        for i in range(25):
            self.a.write(f".claude/skills/foo/ref/{i}.md", f"{i}\n")
        self.a.run()
        shutil.rmtree(self.a.claude / "skills/foo/ref")
        self.a.run()
        self.assertIn("held", self.a.state()["problems"])
        self.assertIn("skills/foo/ref/0.md", self.remote_files())
        self.a.run("--allow-deletes")
        self.assertNotIn("skills/foo/ref/0.md", self.remote_files())
        self.assertNotIn("held", self.a.state()["problems"])

    def test_docket_style_atomic_rewrite_keeps_both_blocks(self):
        path = self.a.claude / "CLAUDE.md"
        doc = path.read_text() + "\n<!-- BEGIN docket (managed) -->\nv1\n<!-- END docket -->\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(doc)
        os.replace(tmp, path)  # what a tool doing write-temp-then-rename does
        self.a.run()
        doc = path.read_text()
        self.assertIn("BEGIN docket", doc)
        self.assertIn(claudemd.BEGIN_PREFIX, doc)
        self.assertNotIn("claude-md", self.a.state()["problems"])
        # and the other tool updating its own block leaves ours alone
        tmp.write_text(doc.replace("v1", "v2"))
        os.replace(tmp, path)
        self.a.run()
        self.assertEqual(path.read_text(), doc.replace("v1", "v2"))

    def test_text_added_outside_blocks_is_flagged(self):
        path = self.a.claude / "CLAUDE.md"
        path.write_text(path.read_text() + "\nnew global rule\n")
        self.a.run()
        self.assertIn("outside any managed block", self.a.hook())

    def test_newer_repo_format_is_refused(self):
        manifest = json.loads((self.a.repo / "agent-sync.json").read_text())
        manifest["format"] = 99
        (self.a.repo / "agent-sync.json").write_text(json.dumps(manifest))
        self.a.run()  # fetch-only recovery, so a fix pushed elsewhere can still arrive
        self.assertIn("repo", self.a.state()["problems"])
        self.assertIn("format 99", self.a.hook())

    def test_dry_run_changes_nothing(self):
        c = Machine(self.tmp, "c")
        c.write(".claude/skills/foo/SKILL.md", "c's own foo\n", old=True)
        c.set_settings({"model": "c-model"})
        before = (c.claude / "settings.json").read_text()
        r = c.run("init", self.url, "--host", "c", "--adopt", "--dry-run", *ID)
        self.assertIn("would", r.stdout)
        self.assertEqual((c.claude / "settings.json").read_text(), before)
        self.assertFalse((c.claude / "skills/foo").is_symlink())
        self.assertFalse((c.home / ".config/agent-sync/config.json").exists())

    def test_adopt_backs_up_a_differing_local_copy(self):
        c = Machine(self.tmp, "c")
        c.write(".claude/skills/foo/SKILL.md", "c's own foo\n", old=True)
        c.run("init", self.url, "--host", "c", "--adopt", *ID)
        self.assertTrue((c.claude / "skills/foo").is_symlink())
        backups = list((c.state_dir / "backups").rglob("SKILL.md"))
        self.assertEqual([p.read_text() for p in backups], ["c's own foo\n"])

    def test_uninstall_leaves_real_copies(self):
        a = self.a
        a.run("uninstall")
        self.assertFalse((a.claude / "skills/foo").is_symlink())
        self.assertIn("v1", a.read(".claude/skills/foo/SKILL.md"))
        doc = a.read(".claude/CLAUDE.md")
        self.assertNotIn(claudemd.BEGIN_PREFIX, doc)
        self.assertIn("Shared setup", doc)
        self.assertIn("A-only rule", doc)
        self.assertNotIn("hook session-start", json.dumps(a.settings()))
        self.assertFalse((a.home / ".config/agent-sync/config.json").exists())



class ReviewFixes(Base):
    """Regression pins for the defects the first fresh-eyes review found."""

    def problems(self, m: Machine) -> dict:
        return m.state()["problems"]

    # sync never fails silently -------------------------------------------------------------------

    def test_unexpected_git_error_becomes_a_visible_problem(self):
        lock_file = self.a.repo / ".git/index.lock"
        lock_file.write_text("")
        self.a.write(".claude/skills/foo/SKILL.md", "v2\n")
        r = self.a.run("--quiet", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error", self.problems(self.a))
        self.assertIn("index.lock", self.a.hook())
        # a lock left by a killed git (old) is cleared, and syncing resumes
        os.utime(lock_file, (OLD, OLD))
        self.a.run()
        self.assertNotIn("error", self.problems(self.a))
        self.assertIn("v2", subprocess.run(["git", "-C", str(self.remote), "show", "main:skills/foo/SKILL.md"],
                                           capture_output=True, text=True).stdout)

    def test_setup_refuses_without_a_git_identity_and_creates_nothing(self):
        c = Machine(self.tmp, "c")
        r = c.run("new", "--host", "c", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--git-email", r.stderr)
        self.assertFalse(c.repo.exists())
        self.assertFalse((c.home / ".config/agent-sync/config.json").exists())

    def test_never_reached_remote_is_reported(self):
        c = Machine(self.tmp, "c")
        c.run("new", "--host", "c", "--remote", str(self.tmp / "typo.git"), *ID)
        self.assertIn("remote", self.problems(c))
        self.assertIn("never reached", c.hook())

    # broken JSON ---------------------------------------------------------------------------------

    def test_broken_base_json_is_held_and_settings_are_kept(self):
        before = self.a.settings()
        (self.a.repo / "settings/base.json").write_text('{"model": "x",}')
        self.a.run()
        self.assertEqual(self.a.settings(), before)
        self.assertIn("held", self.problems(self.a))
        self.assertIn("invalid JSON", self.a.hook())
        self.assertIn("{}", subprocess.run(["git", "-C", str(self.remote), "show", "main:settings/base.json"],
                                           capture_output=True, text=True).stdout)

    def test_broken_base_json_from_elsewhere_leaves_settings_alone(self):
        (self.a.repo / "settings/base.json").write_text(json.dumps({"permissions": {"deny": ["Bash(rm -rf /)"]}}))
        self.a.run()
        self.b.run()
        self.assertIn("deny", json.dumps(self.b.settings()))
        o = self.outsider()
        (o.repo / "settings/base.json").write_text('{"permissions": {"deny": ["Bash(rm -rf /)"],}}')
        self.push_as_outsider(o, "broken edit")
        self.b.run()
        self.assertIn("deny", json.dumps(self.b.settings()))  # not regenerated without the base
        self.assertIn("settings", self.problems(self.b))

    def test_broken_manifest_from_elsewhere_heals_when_fixed_upstream(self):
        o = self.outsider()
        good = (o.repo / "agent-sync.json").read_text()
        (o.repo / "agent-sync.json").write_text('{"format": 1,}')
        self.push_as_outsider(o, "broken manifest")
        self.b.run()
        self.assertIn("repo", self.problems(self.b))
        (o.repo / "agent-sync.json").write_text(good)
        o.write(".local/share/agent-config/skills/foo/SKILL.md", "fixed upstream\n")
        self.push_as_outsider(o, "fix manifest")
        self.b.run()
        self.assertNotIn("repo", self.problems(self.b))
        self.assertEqual(self.b.read(".claude/skills/foo/SKILL.md"), "fixed upstream\n")

    # deletions and history -----------------------------------------------------------------------

    def test_wiping_every_skill_is_held_even_in_a_small_repo(self):
        for name in ("bar", "baz"):
            self.a.write(f".claude/skills/{name}/SKILL.md", f"{name}\n", old=True)
        self.a.run()
        for name in ("foo", "bar", "baz"):
            for f in (self.a.claude / "skills" / name).iterdir():
                f.unlink()  # what `rm -rf ~/.claude/skills/*/` does through the links
        self.a.run()
        self.assertIn("held", self.problems(self.a))
        self.assertIn("skills/bar/SKILL.md", self.remote_files())

    def test_more_than_twenty_deleted_files_is_held_even_when_under_half(self):
        for i in range(60):
            self.a.write(f".local/share/agent-config/notes/n{i}.md", f"{i}\n")
        self.a.run()
        for i in range(25):
            (self.a.repo / f"notes/n{i}.md").unlink()
        self.a.run()
        self.assertIn("held", self.problems(self.a))
        self.assertIn("25 files", self.a.hook())

    def test_history_rewrite_is_not_undone_by_other_machines(self):
        self.a.write(".claude/skills/foo/creds.txt", "password hunter2\n")
        self.a.run()
        self.b.run()
        o = self.outsider()
        o.git("reset", "-q", "--hard", "HEAD~1")
        o.git("push", "-q", "--force", "origin", "HEAD:main")
        self.b.run()
        self.a.run()
        self.assertNotIn("creds.txt", self.remote_files())  # nobody pushed it back
        self.assertIn("rewrite", self.problems(self.b))
        self.assertIn("--accept-rewrite", self.b.hook())
        for m in (self.a, self.b):
            m.run("--accept-rewrite")
            self.assertNotIn("rewrite", self.problems(m))
            self.assertFalse((m.repo / "skills/foo/creds.txt").exists())
        self.assertNotIn("creds.txt", self.remote_files())

    # filesystem ----------------------------------------------------------------------------------

    def test_cloned_skill_is_not_adopted_as_an_empty_submodule(self):
        d = self.a.claude / "skills/cloned"
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / "SKILL.md").write_text("cloned\n")
        age(d)
        self.a.run()
        self.assertFalse(d.is_symlink())
        self.assertNotIn("skills/cloned", self.remote_files())
        self.assertIn(".git", self.a.hook())

    def test_differing_local_copy_is_kept_on_plain_init(self):
        c = Machine(self.tmp, "c")
        c.write(".claude/skills/foo/SKILL.md", "c's own foo\n", old=True)
        c.run("init", self.url, "--host", "c", *ID)
        self.assertFalse((c.claude / "skills/foo").is_symlink())
        self.assertEqual(c.read(".claude/skills/foo/SKILL.md"), "c's own foo\n")
        self.assertIn("links", self.problems(c))
        self.assertNotIn("c's own", subprocess.run(["git", "-C", str(self.remote), "show", "main:skills/foo/SKILL.md"],
                                                   capture_output=True, text=True).stdout)

    def test_case_variant_name_is_not_nested_into_the_shared_skill(self):
        probe = self.tmp / "CaseProbe"
        probe.write_text("")
        if not (self.tmp / "caseprobe").exists():
            self.skipTest("filesystem is case-sensitive")
        c = Machine(self.tmp, "c")
        c.write(".claude/skills/FOO/SKILL.md", "c's own FOO\n", old=True)
        c.run("init", self.url, "--host", "c", *ID)
        self.assertNotIn("FOO", self.remote_files())
        self.assertEqual(c.read(".claude/skills/FOO/SKILL.md"), "c's own FOO\n")

    def test_case_variant_on_a_case_sensitive_filesystem_is_reported_not_adopted(self):
        probe = self.tmp / "CaseProbe2"
        probe.write_text("")
        if (self.tmp / "caseprobe2").exists():
            self.skipTest("filesystem is case-insensitive")
        self.a.write(".claude/skills/FOO/SKILL.md", "a distinct FOO\n", old=True)
        self.a.run()
        self.assertNotIn("skills/FOO", self.remote_files())
        self.assertIn("only in case", self.a.hook())

    def test_skill_json_fixtures_are_content_not_held(self):
        self.a.write(".claude/skills/foo/fixture.json", '{"jsonc": true, // a comment\n}\n')
        self.a.run()
        self.assertNotIn("held", self.problems(self.a))
        self.assertIn("skills/foo/fixture.json", self.remote_files())

    def test_linked_kind_folder_is_left_alone(self):
        dotfiles = self.tmp / "dotfiles/skills"
        (dotfiles / "x").mkdir(parents=True)
        (dotfiles / "x/SKILL.md").write_text("dotfiles x\n")
        age(dotfiles / "x")
        c = Machine(self.tmp, "c")
        os.symlink(dotfiles, c.claude / "skills")
        c.run("init", self.url, "--host", "c", "--adopt", *ID)
        self.assertEqual(sorted(os.listdir(dotfiles)), ["x"])
        self.assertFalse((dotfiles / "x").is_symlink())
        self.assertIn("itself a link", c.hook())

    # CLAUDE.md -----------------------------------------------------------------------------------

    def test_damaged_markers_never_swallow_other_blocks(self):
        path = self.a.claude / "CLAUDE.md"
        docket = "<!-- BEGIN docket (managed by `docket init --agent`; remove with `docket uninstall`) -->\nkeep me\n<!-- END docket -->\n"
        path.write_text(path.read_text().replace(claudemd.END + "\n", "") + "\n" + docket)
        self.a.run()
        self.a.run()
        self.assertIn("keep me", path.read_text())
        self.assertIn("claude-md-broken", self.problems(self.a))
        self.assertIn("markers are damaged", self.a.hook())

    def test_relative_imports_keep_working_after_adoption(self):
        c = Machine(self.tmp, "c")
        c.write(".claude/RTK.md", "rtk rules\n")
        c.write(".claude/CLAUDE.md", "my rule\n@RTK.md\nmail me@example.com\n")
        c.run("init", self.url, "--host", "c", "--adopt", *ID)
        host_md = (c.repo / "hosts/c.md").read_text()
        self.assertIn("@~/.claude/RTK.md", host_md)
        self.assertIn("me@example.com", host_md)


    # settings ------------------------------------------------------------------------------------

    def test_deleting_or_replacing_a_local_secret_sticks(self):
        s = self.a.settings()
        s["env"]["FOO"] = "ghp_" + "a1B2" * 9
        s["other"] = "sk-ant-" + "x" * 30
        self.a.set_settings(s)
        self.a.run()
        self.assertNotIn("ghp_", json.dumps(self.a.hostfile()))
        s = self.a.settings()
        s["env"]["FOO"] = "plain-value"
        del s["other"]
        self.a.set_settings(s)
        self.a.run()
        self.a.run()
        s = self.a.settings()
        self.assertEqual(s["env"]["FOO"], "plain-value")
        self.assertNotIn("other", s)
        self.assertEqual(self.a.hostfile()["settings"]["env"]["FOO"], "plain-value")

    def test_token_count_settings_are_shared_not_secret(self):
        s = self.a.settings()
        s["env"]["MAX_THINKING_TOKENS"] = "31999"
        self.a.set_settings(s)
        self.a.run()
        self.assertEqual(self.a.hostfile()["settings"]["env"]["MAX_THINKING_TOKENS"], "31999")
        self.assertFalse((self.a.state_dir / "local-settings.json").exists())

    def test_user_hook_added_next_to_agent_syncs_is_kept(self):
        s = self.a.settings()
        entry = next(e for e in s["hooks"]["SessionStart"] if "hook session-start" in json.dumps(e))
        entry["hooks"].append({"type": "command", "command": "echo mine"})
        self.a.set_settings(s)
        self.a.run()
        self.a.run()
        self.assertIn("echo mine", json.dumps(self.a.settings()))
        self.assertIn("echo mine", json.dumps(self.a.hostfile()))
        self.assertEqual(json.dumps(self.a.settings()).count("hook session-start"), 1)

    def test_secret_in_a_hook_command_stays_local_and_sync_continues(self):
        s = self.a.settings()
        token = "ghp_" + "a1B2" * 9
        s["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": f"curl -H 'Authorization: Bearer {token}' x"}]}]
        self.a.set_settings(s)
        self.a.run()
        self.assertNotIn("held", self.problems(self.a))
        self.assertNotIn(token, json.dumps(self.a.hostfile()))
        self.assertIn(token, json.dumps(self.a.settings()))
        self.a.write(".claude/skills/later/SKILL.md", "later\n", old=True)
        self.a.run()
        self.assertIn("skills/later/SKILL.md", self.remote_files())

    def test_capture_keeps_this_hosts_existing_overlay(self):
        s = self.a.settings()
        s["env"]["Y"] = "2"
        self.a.set_settings(s)
        self.a.run()
        overlay = self.a.hostfile()["settings"]
        self.assertEqual(overlay["model"], "a-model")
        self.assertEqual(overlay["env"], {"X": "1", "Y": "2"})
        self.assertEqual(overlay["statusLine"]["command"], "{{HOME}}/bin/status")

    def _pull_base_change_without_applying(self, m: Machine) -> None:
        o = self.outsider()
        (o.repo / "settings/base.json").write_text(json.dumps({"effortLevel": "high"}))
        self.push_as_outsider(o, "base: effort high")
        m.git("pull", "-q", "--rebase")  # as if the sync crashed between pull and apply

    def test_base_change_pulled_but_not_applied_is_not_pinned_by_a_local_edit(self):
        self._pull_base_change_without_applying(self.b)
        s = self.b.settings()
        s["model"] = "b2"
        self.b.set_settings(s)
        self.b.run()
        self.assertEqual(self.b.settings()["effortLevel"], "high")
        self.assertEqual(self.b.settings()["model"], "b2")
        self.assertNotIn("effortLevel", self.b.hostfile().get("settings", {}))

    def test_settings_written_but_not_recorded_is_not_pinned(self):
        self._pull_base_change_without_applying(self.b)
        generated = self.b.run("settings", "show").stdout
        (self.b.claude / "settings.json").write_text(generated)  # crash before settings.last.json was saved
        self.b.run()
        self.assertNotIn("effortLevel", self.b.hostfile().get("settings", {}))
        self.assertEqual(self.b.settings()["effortLevel"], "high")



class ReviewFixesRound2(Base):
    """Regression pins for what the second review found in the round-1 fixes."""

    def problems(self, m: Machine) -> dict:
        return m.state()["problems"]

    def remote_show(self, path: str) -> str:
        return subprocess.run(["git", "-C", str(self.remote), "show", f"main:{path}"],
                              capture_output=True, text=True).stdout

    def case_sensitive(self) -> bool:
        (self.tmp / "CaseCheck").write_text("")
        return not (self.tmp / "casecheck").exists()

    # the exchange --------------------------------------------------------------------------------

    def test_force_push_from_a_stale_clone_does_not_silently_drop_pushed_work(self):
        o = self.outsider()  # cloned before the new skill exists
        self.a.write(".claude/skills/newskill/SKILL.md", "new\n", old=True)
        self.a.run()
        self.assertIn("skills/newskill/SKILL.md", self.remote_files())
        o.write(".local/share/agent-config/notes/x.md", "x\n")
        self.push_as_outsider(o, "stale clone", force=True)
        self.a.run()
        self.assertIn("rewrite", self.problems(self.a))
        self.assertTrue((self.a.claude / "skills/newskill/SKILL.md").exists())
        self.a.git("rebase", "-q", "origin/main")  # the hint's "it was a mistake" path
        self.a.run()
        self.assertIn("skills/newskill/SKILL.md", self.remote_files())
        self.assertIn("notes/x.md", self.remote_files())
        self.assertNotIn("rewrite", self.problems(self.a))

    def test_conflict_resolved_with_a_merge_commit_is_pushed(self):
        self.a.write(".claude/commands/hello.md", "A version\n")
        self.b.write(".claude/commands/hello.md", "B version\n")
        self.a.run()
        self.b.run()
        self.assertIn("conflict", self.problems(self.b))
        self.b.git("pull", "-q", "--no-rebase", "origin", "main", ok=False)
        (self.b.repo / "commands/hello.md").write_text("merged\n")
        self.b.git("add", "-A")
        self.b.git("commit", "-q", "-m", "merge")
        self.b.run()
        self.assertNotIn("conflict", self.problems(self.b))
        self.assertEqual(self.remote_show("commands/hello.md"), "merged\n")

    def test_recovery_fast_forwards_even_with_local_edits(self):
        o = self.outsider()
        good = (o.repo / "agent-sync.json").read_text()
        (o.repo / "agent-sync.json").write_text('{"format": 1,}')
        self.push_as_outsider(o, "broken manifest")
        self.b.run()
        self.assertIn("repo", self.problems(self.b))
        self.b.write(".claude/skills/foo/SKILL.md", "b edited while broken\n")  # dirty tree
        (o.repo / "agent-sync.json").write_text(good)
        self.push_as_outsider(o, "fix manifest")
        self.b.run()
        self.assertNotIn("repo", self.problems(self.b))
        self.assertIn("b edited while broken", self.remote_show("skills/foo/SKILL.md"))

    def test_gitlink_already_in_history_does_not_hold_later_syncs(self):
        o = self.outsider()
        sha = o.git("rev-parse", "HEAD").stdout.strip()
        o.git("update-index", "--add", "--cacheinfo", f"160000,{sha},skills/oldlink")
        o.git("commit", "-q", "-m", "gitlink from an older agent-sync")
        o.git("push", "-q", "origin", "HEAD:main")
        self.a.run()
        self.a.write(".claude/skills/foo/SKILL.md", "after the gitlink\n")
        self.a.run()
        self.assertNotIn("held", self.problems(self.a))
        self.assertIn("after the gitlink", self.remote_show("skills/foo/SKILL.md"))

    def test_new_gitlink_staged_directly_in_the_repo_is_held(self):
        nested = self.a.repo / "skills/cloned2"
        nested.mkdir()
        (nested / "SKILL.md").write_text("cloned2\n")
        mail = "t" + "@" + "example.com"
        env = dict(self.a.env, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL=mail,
                   GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL=mail)
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(nested), *cmd], check=True, env=env, capture_output=True)
        self.a.run()
        self.assertIn("held", self.problems(self.a))
        self.assertIn("rm -r -q --cached", self.a.hook())
        self.assertNotIn("skills/cloned2", self.remote_files())

    # own JSON ------------------------------------------------------------------------------------

    def test_base_json_that_is_not_an_object_or_is_deleted_is_held(self):
        (self.a.repo / "settings/base.json").write_text("[]")
        self.a.run()
        self.assertIn("held", self.problems(self.a))
        (self.a.repo / "settings/base.json").unlink()
        self.a.run()
        self.assertIn("deleted", self.a.hook())
        self.assertIn("{}", self.remote_show("settings/base.json"))

    def test_host_settings_that_are_not_an_object_leave_settings_alone(self):
        before = self.b.settings()
        o = self.outsider()
        (o.repo / "hosts/b.json").write_text(json.dumps({"settings": []}))
        self.push_as_outsider(o, "bad overlay")
        self.b.run()
        self.assertEqual(self.b.settings(), before)
        self.assertIn("settings", self.problems(self.b))

    # setup ---------------------------------------------------------------------------------------

    def test_init_refuses_without_identity_before_cloning(self):
        c = Machine(self.tmp, "c")
        r = c.run("init", self.url, "--host", "c", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(c.repo.exists())

    # secrets -------------------------------------------------------------------------------------

    def test_secret_named_settings_without_a_token_shape_stay_local(self):
        s = self.a.settings()
        s["env"]["SECRET_KEY"] = "django-insecure-k#9x!m@2p$q(r)s&t*u"
        s["env"]["DB_PASSWORD_PROD"] = "Tr0ub4dor&3"
        self.a.set_settings(s)
        self.a.run()
        self.assertNotIn("Tr0ub4dor", self.remote_show("hosts/a.json"))
        self.assertNotIn("django-insecure", json.dumps(self.a.hostfile()))
        self.assertEqual(self.a.settings()["env"]["DB_PASSWORD_PROD"], "Tr0ub4dor&3")

    # names that differ only in case --------------------------------------------------------------

    def test_case_only_rename_of_a_shared_skill_keeps_syncing(self):
        self.a.git("mv", "skills/foo", "skills/foo-tmp")  # two steps: macOS can't rename by case alone
        self.a.git("mv", "skills/foo-tmp", "skills/Foo")
        for _ in range(3):
            self.a.run()
            self.b.run()
        for m in (self.a, self.b):
            self.assertNotIn("error", self.problems(m), m.name)
            self.assertIn("v1", m.read(".claude/skills/Foo/SKILL.md"))
        self.b.write(".claude/commands/hello.md", "still syncing\n")
        self.b.run()
        self.assertEqual(self.remote_show("commands/hello.md"), "still syncing\n")

    def test_case_twins_created_together_adopt_only_one(self):
        if not self.case_sensitive():
            self.skipTest("filesystem is case-insensitive")
        self.a.write(".claude/skills/bar/SKILL.md", "bar\n", old=True)
        self.a.write(".claude/skills/BAR/SKILL.md", "BAR\n", old=True)
        self.a.run()
        files = self.remote_files()
        self.assertEqual(int("skills/bar/SKILL.md" in files) + int("skills/BAR/SKILL.md" in files), 1)
        self.assertIn("only in case", self.a.hook())


if __name__ == "__main__":
    unittest.main()
