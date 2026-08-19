"""Unit tests for k8s-operator/scripts/installer_common.sh helpers.

Covers the Terraform-state cluster probe (a managed-mode cluster entry reads
as "ours", a data-mode entry from an existing-cluster install does not, and
unparseable or unreadable state fails safe), the comma-or-space splitting
behind --custom-roles, and the API_SERVER_KEY guard in the tfvars generator.
"""

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALLER_COMMON = _REPO_ROOT / "k8s-operator" / "scripts" / "installer_common.sh"

# installer_common.sh's contract: the caller defines the print helpers.
_PRINT_STUBS = """
print_info() { :; }
print_success() { :; }
print_warning() { :; }
print_error() { echo "ERROR: $*" >&2; }
"""


def _state_doc(resources):
    return json.dumps({"version": 4, "resources": resources})


MANAGED_CLUSTER_STATE = _state_doc(
    [{"mode": "managed", "type": "google_container_cluster", "name": "standard"}]
)
DATA_MODE_STATE = _state_doc(
    [{"mode": "data", "type": "google_container_cluster", "name": "existing"}]
)


class InstallerCommonTest(unittest.TestCase):
    def _run(self, script, gcloud_stdout=None, gcloud_exit=0, env=None, kubectl_script=None):
        """Source installer_common.sh with print stubs and run `script`.

        A stub `gcloud` on PATH prints `gcloud_stdout` (when given) and exits
        `gcloud_exit`, standing in for `gcloud storage cat` on the state
        object.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            state_file = pathlib.Path(tmp) / "default.tfstate"
            if gcloud_stdout is not None:
                state_file.write_text(gcloud_stdout)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"[ -f '{state_file}' ] && cat '{state_file}'\n"
                f"exit {gcloud_exit}\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            # Hermetic kubectl: the generator recovers credentials from the
            # live Secret when it can, and a developer's real kube context
            # must never answer a unit test.
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(kubectl_script or "#!/usr/bin/env bash\nexit 1\n")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            full_env = get_isolated_test_env(
                overrides={
                    "PROJECT_ID": "test-project",
                    "CLUSTER_NAME": "test-cluster",
                    "REGION": "us-central1",
                    **(env or {}),
                },
                bin_dir=str(bin_dir),
            )
            body = f'set -u\n{_PRINT_STUBS}\nsource "{_INSTALLER_COMMON}"\n{script}'
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    # ── tf_state_has_cluster: the create_cluster re-run probe ────────────────

    def test_managed_cluster_entry_reads_as_ours(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=MANAGED_CLUSTER_STATE,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_data_mode_entry_is_not_ours(self):
        # An existing-cluster install records a data-mode entry in the same
        # state; reading it as "ours" would flip create_cluster back to true
        # on re-run and plan a second cluster over the real one.
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=DATA_MODE_STATE,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    def test_unparseable_state_fails_safe(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout="this is not JSON {",
        )
        self.assertNotIn("rc=0", proc.stdout, proc.stderr)

    def test_unreadable_state_fails_safe(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=None,
            gcloud_exit=1,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    # ── hcl_csv_list: --custom-roles documents "space- or comma-separated" ──

    def test_csv_list_splits_on_commas(self):
        proc = self._run('hcl_csv_list "roles/viewer,roles/monitoring.viewer"')
        self.assertEqual(
            proc.stdout, '["roles/viewer", "roles/monitoring.viewer"]', proc.stderr
        )

    def test_csv_list_splits_on_spaces(self):
        proc = self._run('hcl_csv_list "roles/viewer roles/monitoring.viewer"')
        self.assertEqual(
            proc.stdout, '["roles/viewer", "roles/monitoring.viewer"]', proc.stderr
        )

    def test_csv_list_splits_mixed_and_trims(self):
        proc = self._run('hcl_csv_list " roles/a , roles/b  roles/c "')
        self.assertEqual(proc.stdout, '["roles/a", "roles/b", "roles/c"]', proc.stderr)

    def test_csv_list_empty_input_is_empty_list(self):
        proc = self._run('hcl_csv_list ""')
        self.assertEqual(proc.stdout, "[]", proc.stderr)

    # ── write_tfvars_from_state: the API_SERVER_KEY guard ────────────────────

    def test_tfvars_generation_without_api_server_key_fails_with_guidance(self):
        # vars.sh omits API_SERVER_KEY when PERSIST_SECRETS_ON_DISK=false
        # stripped it; under the front doors' `set -u` an unguarded read would
        # abort on an opaque unbound-variable error mid-run.
        proc = self._run(
            "set -Eeo pipefail\n"
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"'
        )
        self.assertNotIn("rc=0", proc.stdout)
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertIn("API_SERVER_KEY", proc.stderr)

    def test_tfvars_generation_recovers_credentials_from_live_secret(self):
        # PERSIST_SECRETS_ON_DISK=false leaves vars.sh without the keys; the
        # generator reads them back from the live Secret, as provision_07 did.
        recovered_b64 = "cmVjb3ZlcmVkLWtleQ=="  # base64("recovered-key")
        kubectl_stub = (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f'  *"get secret platform-agent-secrets"*) printf "%s" "{recovered_b64}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                kubectl_script=kubectl_stub,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('api_server_key    = "recovered-key"', dest.read_text())


if __name__ == "__main__":
    unittest.main()
