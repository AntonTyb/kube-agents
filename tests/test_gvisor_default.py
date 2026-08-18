"""Tests that GKE Sandbox (gVisor) isolation is on by default, in every place that decides it.

    python3 -m unittest discover -s tests -p 'test_*.py'

The default lives in four surfaces that have to agree: `common.sh`'s
`DEFAULT_ENABLE_GVISOR`, the two provisioning steps that read it, the CR template that
ships the `runtimeClassName` field, and `install.sh`, which is the front door the
quickstart sends people to and which writes its answer into `vars.sh` — where it then
outranks every script default, because `load_state` sources that file first.

Nothing else covers any of it. There is no shell test harness in this repository, the Go
suite does not reach `k8s-operator/scripts/`, and the operator's own tests only exercise a
`RuntimeClassName` their fixtures set by hand. So a revert here is silent: the field goes
back to being commented out, or one entry point keeps defaulting to `false`, and every
check in CI still passes while new installs quietly run the agent on the host kernel.

The opt-out path is pinned the same way, because it is the fragile half. `provision_08`
turns the field off with one `sed` bound to the exact shape of one template line; rename
the field, reindent that block, or append a `${...}` to it and the substitution stops
matching, leaving `ENABLE_GVISOR=false` to apply a CR that requests a sandbox the cluster
was deliberately configured not to have. These tests run the script's own `sed`, read out
of the script rather than restated here, so the two cannot drift.

Teardown is the deliberate exception and is asserted as one: see
`teardown_02_gvisor_nodepool.sh`.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "k8s-operator" / "scripts"
_COMMON = _SCRIPTS / "common.sh"
_PROVISION_02 = _SCRIPTS / "provision_02_gvisor_nodepool.sh"
_PROVISION_08 = _SCRIPTS / "provision_08_deploy_platform_agent.sh"
_TEARDOWN_02 = _SCRIPTS / "teardown_02_gvisor_nodepool.sh"
_CR_TEMPLATE = _SCRIPTS / "platform-agent.yaml.template"
_INSTALL = _REPO / "install.sh"


def _render_template() -> str:
    """The CR manifest as `provision_08` renders it, through the same `envsubst`.

    Every `${VAR}` gets a value, because envsubst substitutes an unset one with the
    empty string and `namespace: ""` is a different document from the one the script
    produces. The values only have to keep the YAML parseable — nothing here asserts on
    them — so the `*_ENABLED` booleans are the only ones that need a specific shape.
    """
    template = _CR_TEMPLATE.read_text()
    env = {}
    for name in set(re.findall(r"\$\{([A-Z0-9_]+)\}", template)):
        env[name] = "true" if name.endswith("_ENABLED") else f"test-{name.lower()}"
    return subprocess.run(
        ["envsubst"],
        input=template,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _availability(manifest: str) -> dict:
    return yaml.safe_load(manifest)["spec"]["deployment"]["availability"]


class GvisorDefaultTest(unittest.TestCase):
    """The default is on, and it is spelled once."""

    def test_the_shared_default_is_true(self):
        self.assertRegex(
            _COMMON.read_text(),
            re.compile(r'^DEFAULT_ENABLE_GVISOR="true"$', re.MULTILINE),
            msg="common.sh no longer defaults GKE Sandbox isolation on",
        )

    def test_both_provisioning_steps_read_the_shared_default(self):
        # A literal here would be a second home for the default, which is how the
        # installer and the scripts drifted apart before.
        for script in (_PROVISION_02, _PROVISION_08):
            with self.subTest(script=script.name):
                self.assertIn(
                    'init_var "ENABLE_GVISOR" "$DEFAULT_ENABLE_GVISOR"',
                    script.read_text(),
                    msg=f"{script.name} spells the gVisor default itself instead of reading common.sh",
                )

    def test_the_installer_reads_the_shared_default(self):
        install = _INSTALL.read_text()
        self.assertIn(
            'PARAM_ENABLE_GVISOR="${PARAM_ENABLE_GVISOR:-$DEFAULT_ENABLE_GVISOR}"',
            install,
            msg="install.sh does not resolve --gvisor from common.sh's DEFAULT_ENABLE_GVISOR",
        )
        # install.sh is the front door and its answer is written to vars.sh, which
        # load_state sources ahead of every script default. A `false` fallback left
        # anywhere on that path silently outranks the flip.
        self.assertNotIn(
            '${ENABLE_GVISOR:-false}',
            install,
            msg="install.sh still falls back to gVisor off somewhere on the parameter path",
        )
        self.assertNotIn(
            '${PARAM_ENABLE_GVISOR:-false}',
            install,
            msg="install.sh still falls back to gVisor off somewhere on the parameter path",
        )

    def test_the_interactive_prompt_defaults_to_the_sandbox(self):
        # prompt_menu's default answer is always option 1, so which option is listed
        # first *is* the default for anyone who presses enter.
        install = _INSTALL.read_text()
        menu = re.search(
            r'prompt_menu "Enable GKE Sandbox[^"]*"[^\n]*\n\s*"([^"]*)"\s*\\\n\s*"([^"]*)"',
            install,
        )
        self.assertIsNotNone(menu, "the gVisor prompt is no longer a two-option prompt_menu")
        self.assertTrue(
            menu.group(1).startswith("Yes"),
            f"the enter-key answer is {menu.group(1)!r}, which does not enable the sandbox",
        )

    def test_teardown_keeps_the_opposite_default(self):
        # Provision and teardown want opposite defaults: a pool nobody asked for is
        # cheap to delete, a pool somebody wanted is not cheap to lose. This asserts
        # the asymmetry is still deliberate rather than an oversight, since
        # `teardown.sh --no-confirm` from a clone with no vars.sh would otherwise
        # delete any node pool named gvisor-pool without asking.
        teardown = _TEARDOWN_02.read_text()
        self.assertIn('is_truthy "${ENABLE_GVISOR:-false}"', teardown)
        # Comments are stripped first: the script names DEFAULT_ENABLE_GVISOR on
        # purpose, to say it is not using it.
        code = "\n".join(
            line for line in teardown.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn(
            "DEFAULT_ENABLE_GVISOR",
            code,
            msg="teardown now shares the provisioning default, so a stateless clone "
            "would delete a gVisor node pool it does not know it owns",
        )


class CustomResourceTemplateTest(unittest.TestCase):
    """What the rendered CR actually asks the operator for."""

    def setUp(self):
        if shutil.which("envsubst") is None:
            self.fail(
                "envsubst is missing; provision_08 renders the CR with it, so this "
                "cannot be checked any other way"
            )
        self.manifest = _render_template()

    def test_the_rendered_cr_requests_the_sandbox(self):
        self.assertEqual(
            _availability(self.manifest).get("runtimeClassName"),
            "gvisor",
            msg="the rendered PlatformAgent no longer requests the gvisor RuntimeClass",
        )

    def test_the_opt_out_comments_the_field_out(self):
        # Runs the script's own sed rather than a copy of it, so a change to the
        # substitution that stops matching the template fails here.
        with tempfile.TemporaryDirectory() as tmp:
            rendered = pathlib.Path(tmp) / "platform-agent.yaml"
            rendered.write_text(self.manifest)
            subprocess.run(
                ["bash", "-c", f'CR_MANIFEST="{rendered}"; {self._disable_command()}'],
                check=True,
                capture_output=True,
                text=True,
            )
            disabled = rendered.read_text()

        self.assertNotIn(
            "runtimeClassName",
            _availability(disabled),
            msg="ENABLE_GVISOR=false left the runtimeClassName field active in the CR",
        )
        self.assertIn(
            "# runtimeClassName: gvisor",
            disabled,
            msg="the field was removed rather than commented out; the opt-out no longer "
            "leaves a readable record of what it turned off",
        )
        self.assertFalse(
            list(pathlib.Path(disabled).parent.glob("*.bak"))
            if pathlib.Path(disabled).parent.exists()
            else [],
            msg="the sed backup file was left behind",
        )

    def test_there_is_no_dead_enable_arm(self):
        # The template ships the field enabled, so a substitution turning
        # `# runtimeClassName` into `runtimeClassName` can never match. One used to
        # live here and logged as though it had acted.
        self.assertNotRegex(
            _PROVISION_08.read_text(),
            r"sed[^\n]*s/# runtimeClassName",
            msg="provision_08 has an enable-arm sed again; it cannot match a template "
            "that already ships the field uncommented",
        )

    def _disable_command(self):
        """The one `sed` provision_08 runs to honour ENABLE_GVISOR=false."""
        seds = [
            line.strip()
            for line in _PROVISION_08.read_text().splitlines()
            if line.strip().startswith("sed ") and "runtimeClassName" in line
        ]
        self.assertEqual(
            len(seds),
            1,
            msg=f"expected exactly one runtimeClassName sed in provision_08, found {seds}",
        )
        return seds[0]


class RuntimeClassProbeTest(unittest.TestCase):
    """The step must not apply a CR the operator will refuse to reconcile."""

    def test_provision_08_probes_for_the_runtimeclass(self):
        # Without this the redeploy workflows (reusable-deploy-agent.yml runs step 08
        # alone, never step 02) apply a CR naming a RuntimeClass their clusters do not
        # have, the operator early-returns Degraded/RuntimeClassNotFound above
        # reconcileWorkload, and the generation gate below burns AGENT_READY_TIMEOUT
        # before failing.
        script = _PROVISION_08.read_text()
        self.assertIn(
            "kubectl get runtimeclass gvisor",
            script,
            msg="provision_08 no longer checks whether the cluster has the gvisor RuntimeClass",
        )
        # An explicit request must fail rather than be silently downgraded; only the
        # default may fall back.
        self.assertIn("GVISOR_EXPLICIT", script)
        probe = script[script.index("kubectl get runtimeclass gvisor") :]
        self.assertLess(
            probe.index("return 1"),
            probe.index('ENABLE_GVISOR="false"'),
            msg="an explicit ENABLE_GVISOR=true no longer fails when the RuntimeClass is absent",
        )


class GvisorExplicitFlagTest(unittest.TestCase):
    """`GVISOR_EXPLICIT` must survive a re-run of the step on its own.

    The fallback above is only safe while "the cluster has no gvisor RuntimeClass" and
    "somebody asked for gVisor" stay independent. They stop being independent the moment
    the step persists what it defaulted: `init_var` calls `save_var`, `save_var` appends
    to `vars.sh`, and `load_state` sources `vars.sh` before the flag is computed — so one
    silent fallback teaches the next run that the value was a request, and a redeploy
    that changed nothing about the cluster hard-fails. CI never sees it because every
    workflow starts from a fresh checkout and throws `vars.sh` away; a self-hosted
    runner, a Cloud Shell session or a laptop does not.

    These run the script's own resolution block against the real `common.sh`, so they
    fail if either side of that interaction moves.
    """

    def _resolution_block(self) -> str:
        """The lines of `provision_08` that resolve ENABLE_GVISOR and GVISOR_EXPLICIT."""
        lines = _PROVISION_08.read_text().splitlines()
        starts = [i for i, line in enumerate(lines) if line.strip() == "GVISOR_EXPLICIT=0"]
        self.assertEqual(
            len(starts),
            1,
            msg="expected exactly one GVISOR_EXPLICIT=0 in provision_08",
        )
        start = starts[0]
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi")
        block = "\n".join(lines[start : end + 1])
        # Resolving the value outside the branch that computes the flag is exactly the
        # shape that persists a default, so the extraction refuses to run against it
        # rather than reporting an empty ENABLE_GVISOR as some other failure.
        self.assertIn(
            'init_var "ENABLE_GVISOR"',
            block,
            msg="provision_08 resolves ENABLE_GVISOR outside the GVISOR_EXPLICIT "
            "branch; an unconditional init_var persists the default it picks",
        )
        return block

    def _resolve(self, vars_file: pathlib.Path, **env) -> tuple[str, str]:
        """Run the block once, the way a non-interactive step 08 would."""
        script = "\n".join(
            [
                "set -e",
                f'source "{_COMMON}"',
                # What load_state does with a vars.sh that already exists. The rest of
                # load_state wants a live gcloud, and none of it touches ENABLE_GVISOR.
                'if [ -f "$VARS_FILE" ]; then source "$VARS_FILE"; fi',
                self._resolution_block(),
                'echo "RESULT ${ENABLE_GVISOR} ${GVISOR_EXPLICIT}"',
            ]
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "VARS_FILE": str(vars_file),
                "CI": "true",  # is_non_interactive, as every scripted install is
                **env,
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        line = next(
            ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")
        )
        _, enabled, explicit = line.split()
        return enabled, explicit

    def test_a_defaulted_value_is_not_persisted_and_does_not_latch(self):
        with tempfile.TemporaryDirectory() as tmp:
            vars_file = pathlib.Path(tmp) / "vars.sh"

            self.assertEqual(self._resolve(vars_file), ("true", "0"))
            self.assertNotIn(
                "ENABLE_GVISOR",
                vars_file.read_text() if vars_file.exists() else "",
                msg="step 08 wrote the value it defaulted into vars.sh; the next run "
                "will read its own fallback back as an explicit request",
            )
            self.assertEqual(
                self._resolve(vars_file),
                ("true", "0"),
                msg="the second run of the same step reports the default as explicit",
            )

    def test_an_exported_value_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vars_file = pathlib.Path(tmp) / "vars.sh"
            self.assertEqual(
                self._resolve(vars_file, ENABLE_GVISOR="true"),
                ("true", "1"),
            )

    def test_a_saved_value_is_explicit(self):
        # install.sh writes the installer's answer into vars.sh, and provision_02 saves
        # the value before it creates the pool. Both are choices; a missing RuntimeClass
        # afterwards is an error rather than a reason to drop the sandbox.
        with tempfile.TemporaryDirectory() as tmp:
            vars_file = pathlib.Path(tmp) / "vars.sh"
            vars_file.write_text("export ENABLE_GVISOR=true\n")
            self.assertEqual(self._resolve(vars_file), ("true", "1"))


if __name__ == "__main__":
    unittest.main()
