"""Unit tests for profile_scaffold.overlay_template (the image -> PVC force-sync).

Run: python3 -m unittest agents.platform.scripts.test_profile_scaffold

This is the mechanism the entrypoint uses to keep an EXISTING platform profile
tracking the image. It replaced a `for f in ...; do [ -f ... ] && cp -f; done`
loop, which could only ever copy files: `[ -f ]` is false for a directory, so
cron/, skills/, and governance/ were silently skipped on every upgrade and the
agent shipped a CAPABILITIES.md describing machinery that was not there. The
directory cases below are that regression, not a formality.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_scaffold as ps  # noqa: E402

ITEMS = ("config.yaml", "SOUL.md", "AGENTS.md", "CAPABILITIES.md", "cron", "skills", "governance")


def job(job_id, **extra):
    """A cron entry shaped like the ones agents/platform/cron/jobs.json ships."""
    return {
        "id": job_id,
        "name": job_id.replace("-", " ").title(),
        "schedule": {"kind": "cron", "expr": "20 6 * * *"},
        "prompt": f"run {job_id}",
        "enabled": True,
        **extra,
    }


class OverlayTemplateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.template = root / "template"
        self.home = root / "home"
        for path in (self.template, self.home):
            path.mkdir()
        write(self.template / "SOUL.md", "image persona")
        write(self.template / "CAPABILITIES.md", "image capabilities")
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.template / "skills" / "fleet-audit" / "SKILL.md", "image skill")
        write(self.template / "governance" / "compliance_audit_sop.md", "image sop")

    def tearDown(self):
        self.temp_dir.cleanup()

    def jobs_on_disk(self):
        return json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]

    def test_named_directories_are_overlaid_not_skipped(self):
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual(["audit"], [j["id"] for j in self.jobs_on_disk()])
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())
        self.assertEqual("image sop", (self.home / "governance" / "compliance_audit_sop.md").read_text())

    def test_the_image_copy_wins_over_what_is_already_on_the_volume(self):
        write(self.home / "SOUL.md", "stale persona")
        write(self.home / "skills" / "fleet-audit" / "SKILL.md", "stale skill")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("image persona", (self.home / "SOUL.md").read_text())
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())

    def test_runtime_state_and_removed_entries_survive(self):
        # The overlay adds and overwrites; it never prunes. Per-profile runtime
        # state has to survive an upgrade, and the price of that guarantee is
        # that a skill withdrawn from the image also survives — a known limit
        # recorded at the sync site in deploy/shared/docker-entrypoint.sh.
        write(self.home / "USER.md", "operator notes")
        write(self.home / "skills" / "withdrawn" / "SKILL.md", "no longer in the image")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("operator notes", (self.home / "USER.md").read_text())
        self.assertTrue((self.home / "skills" / "withdrawn" / "SKILL.md").exists())

    def test_an_item_the_template_does_not_ship_is_not_an_error(self):
        # config.yaml and AGENTS.md are named by the entrypoint but a template
        # need not carry every one of them.
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertFalse((self.home / "config.yaml").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())

    def test_no_item_list_overlays_the_whole_template(self):
        ps.overlay_template(self.home, self.template)
        self.assertTrue((self.home / "cron" / "jobs.json").exists())
        self.assertTrue((self.home / "CAPABILITIES.md").exists())

    def test_a_missing_template_is_a_hard_error(self):
        with self.assertRaises(SystemExit):
            ps.overlay_template(self.home, self.template.parent / "absent", None, ITEMS)


class CronStoreMergeTest(unittest.TestCase):
    """cron/jobs.json is image-owned configuration and runtime state at once.

    A plain copytree took both. Every restart wiped each job's run history, so a
    daily audit that had already fired at 06:20 fired again after a rollout, and
    a job the operator had added to the store simply disappeared. These pin the
    per-key rule the merge replaced it with.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.template = root / "template"
        self.home = root / "home"
        for path in (self.template, self.home):
            path.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def overlay(self, image_jobs, live_jobs):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": image_jobs}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": live_jobs}))
        ps.overlay_template(self.home, self.template, None, ("cron",))
        return json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]

    def test_run_history_survives_the_upgrade(self):
        merged = self.overlay(
            [job("audit", prompt="new prompt")],
            [job("audit", prompt="old prompt", last_run="2026-08-03T06:20:00Z")],
        )
        self.assertEqual(1, len(merged))
        self.assertEqual("new prompt", merged[0]["prompt"])
        self.assertEqual("2026-08-03T06:20:00Z", merged[0]["last_run"])

    def test_the_image_decides_whether_a_watchdog_is_enabled(self):
        # "Edit cron/jobs.json, flip enabled to false, and redeploy" is the
        # documented way to turn a watchdog off. A merge that let the volume
        # keep its own `enabled` would make that instruction a no-op on every
        # cluster that had already run the job once.
        merged = self.overlay([job("audit", enabled=False)], [job("audit", enabled=True)])
        self.assertIs(False, merged[0]["enabled"])

    def test_a_job_the_image_does_not_ship_is_kept(self):
        merged = self.overlay([job("audit")], [job("audit"), job("operator-added")])
        self.assertEqual(["audit", "operator-added"], [j["id"] for j in merged])

    def test_a_job_withdrawn_from_the_image_is_not_pruned(self):
        # The cost of the rule above, stated rather than discovered: nothing
        # here can tell an operator's own job from one this release deleted,
        # and the overlay does not prune. Removing an entry from the shipped
        # jobs.json therefore does not stop it firing on a cluster that already
        # has it. Shipping it with `enabled: false` does — which is why the
        # five retired watchdogs are still listed in agents/platform/cron/.
        merged = self.overlay([job("audit")], [job("audit"), job("withdrawn")])
        self.assertEqual(["audit", "withdrawn"], [j["id"] for j in merged])

    def test_a_new_job_arrives_with_no_prior_state(self):
        merged = self.overlay([job("audit"), job("brand-new")], [job("audit")])
        self.assertEqual(["audit", "brand-new"], [j["id"] for j in merged])

    def test_a_first_scaffold_just_takes_the_image(self):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(["audit"], [j["id"] for j in store["jobs"]])

    def test_an_unreadable_store_lets_the_image_copy_stand(self):
        # Half a jobs.json is not a reason to leave the profile with no cron
        # roster at all.
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.home / "cron" / "jobs.json", '{"jobs": [{"id": "aud')
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(["audit"], [j["id"] for j in store["jobs"]])

    def test_top_level_keys_follow_the_same_rule(self):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [], "version": 2}))
        write(
            self.home / "cron" / "jobs.json",
            json.dumps({"jobs": [], "version": 1, "scheduler_state": {"tick": 9}}),
        )
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(2, store["version"])
        self.assertEqual({"tick": 9}, store["scheduler_state"])

    def test_the_merge_is_skipped_when_cron_is_not_being_overlaid(self):
        # `--items` without `cron` means the caller is not touching the cron
        # directory; rewriting jobs.json anyway would be this function editing
        # a file it was not asked to.
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.template / "SOUL.md", "image persona")
        original = json.dumps({"jobs": [job("only-on-the-volume")]})
        write(self.home / "cron" / "jobs.json", original)
        ps.overlay_template(self.home, self.template, None, ("SOUL.md",))
        self.assertEqual(original, (self.home / "cron" / "jobs.json").read_text())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
