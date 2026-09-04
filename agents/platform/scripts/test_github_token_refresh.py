import email.message
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import github_token_refresh
from github_token_refresh import (
    get_current_git_repo,
    main,
    refresh_git_credentials,
)


class GitHubTokenRefreshTest(unittest.TestCase):
    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_https(self, run):
        res = MagicMock()
        res.stdout = "https://github.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_ssh(self, run):
        res = MagicMock()
        res.stdout = "git@github.com:gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_ssh_over_443(self, run):
        res = MagicMock()
        res.stdout = "ssh://git@ssh.github.com:443/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_rejects_lookalike_hosts(self, run):
        res = MagicMock()
        run.return_value = res
        for url in (
            "https://evil.example/github.com/gke-labs/kube-agents.git",
            "https://github.com.evil.example/gke-labs/kube-agents.git",
            "https://notgithub.com/gke-labs/kube-agents.git",
            "git@evil.example:github.com/gke-labs/kube-agents.git",
            "https://github.com@evil.example/gke-labs/kube-agents.git",
            "https://evil.example/x.git?github.com",
            "https://evil.example/x.git#github.com",
        ):
            with self.subTest(url=url):
                res.stdout = url + "\n"
                self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_www_alias(self, run):
        res = MagicMock()
        res.stdout = "https://www.github.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_rejects_non_slug_paths(self, run):
        res = MagicMock()
        run.return_value = res
        for url in (
            # A deep link, not a clone URL: nothing downstream on the direct
            # Minty path would reject "kube-agents/tree/main" as a repository.
            "https://github.com/gke-labs/kube-agents/tree/main",
            "https://github.com/../../etc/passwd",
            "https://github.com/%2e%2e/x.git",
            "https://github.com/gke-labs",
            "https://github.com/",
        ):
            with self.subTest(url=url):
                res.stdout = url + "\n"
                self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_non_github_remote_returns_none(self, run):
        res = MagicMock()
        res.stdout = "https://gitlab.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_local_path_returns_none(self, run):
        res = MagicMock()
        res.stdout = "/srv/git/kube-agents.git\n"
        run.return_value = res
        self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_failure_returns_none(self, run):
        run.side_effect = Exception("git not found")
        self.assertIsNone(get_current_git_repo())

    def test_refresh_git_credentials_invalid_repo_raises(self):
        with patch("github_token_refresh.get_current_git_repo", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("")
            self.assertIn("Could not identify target repository", str(cm.exception))

        with self.assertRaises(RuntimeError) as cm:
            refresh_git_credentials("invalid-repo-no-slash")
        self.assertIn("Could not identify target repository", str(cm.exception))

    def test_refresh_git_credentials_refuses_a_host_shaped_repository(self):
        """The slug gate, not a slash count.

        The check this replaced counted separators, so every value here passed
        it and reached the Minty branch below, which splits on the first slash
        and posts the left half as an org name: `github.com/acme` would have
        been minted for an org called `github.com`. Nothing downstream of that
        branch validates, so these have to fail here or not at all.

        `" acme/toolkit "` is deliberately absent: the line above the gate
        strips whitespace and slashes and it is the *stripped* value that goes
        on to the broker, so normalising it there is safe. What is not safe is
        normalising and then posting the original, which is why
        `credential_proxy` gets the strict predicate.
        """
        for repository in (
            "github.com/acme",
            "www.github.com/acme",
            "ssh.github.com/acme",
            "acme/..",
            "acme/-toolkit",
            "acme/toolkit.git",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials(repository)
                self.assertIn(
                    "Could not identify target repository", str(cm.exception)
                )

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_delegates_without_receiving_token(self, urlopen, run):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("", token)
        run.assert_not_called()
        request = urlopen.call_args.args[0]
        self.assertEqual(
            "http://127.0.0.1:8765/v1/forge/refresh", request.full_url
        )
        # The provider is a body field, not a path segment. A path per forge
        # would mean the route table grows with the roster and an agent image
        # has to know which routes its sidecar serves.
        self.assertEqual("github", json.loads(request.data)["provider"])

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_non_github_provider_is_refused_before_minty_is_asked(self, urlopen, run):
        """This script is GitHub's token-acquisition strategy, not a pipeline
        every forge is fitted into: Minty mints GitHub App installation tokens
        from a signed JWT, and a GitLab access token needs no minting step at
        all. Proceeding would ask Minty for a GitHub token in the name of a
        repository that is not on GitHub, so a second forge gets its own
        refresher and the sidecar dispatches between them.

        The direct path, with `CREDENTIAL_PROXY_URL` cleared explicitly rather
        than assumed absent from the runner's environment. That is the path the
        guard governs: in the sandbox the script is a transport and the sidecar
        is what refuses a provider it does not serve.
        """
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", provider="gitlab")

        self.assertIn("gitlab", str(cm.exception))
        urlopen.assert_not_called()
        run.assert_not_called()

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_sidecar_without_the_new_route_is_retried_on_the_old_one(
        self, urlopen, run
    ):
        """Skew in the direction the alias does not cover.

        The sidecar serving `/v1/github/refresh` handles an old agent against a
        new sidecar. This is the reverse — a new agent against a sidecar that
        predates the rename, which the operator permits indefinitely because
        `CREDENTIAL_PROXY_IMAGE` pins the sidecar independently of the agent
        tag. Without the retry that pair loses every credential refresh, and so
        every git write the agent makes.
        """
        ok = MagicMock()
        ok.__enter__.return_value.status = 200
        urlopen.side_effect = [
            urllib.error.HTTPError(
                "http://127.0.0.1:8765/v1/forge/refresh", 404, "Not Found", {}, None
            ),
            ok,
        ]

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            self.assertEqual("", refresh_git_credentials("owner/repository"))

        self.assertEqual(
            [
                "http://127.0.0.1:8765/v1/forge/refresh",
                "http://127.0.0.1:8765/v1/github/refresh",
            ],
            [call.args[0].full_url for call in urlopen.call_args_list],
        )
        run.assert_not_called()

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_genuine_refusal_is_never_sent_twice(self, urlopen, run):
        """The fallback is conditioned on 404 alone.

        A sidecar that serves the route answers 200, 400 or 502, so retrying
        anything but a 404 would put every real refusal — an unconfigured
        provider, a malformed repository, a Minty outage — on the wire a second
        time.
        """
        urlopen.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh", 400, "Bad Request", {}, None
        )

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                refresh_git_credentials("owner/repository")

        self.assertEqual(1, urlopen.call_count)

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_another_forge_is_never_retried_on_the_github_route(self, urlopen, run):
        """A sidecar old enough to lack `/v1/forge/refresh` is old enough to
        serve GitHub and nothing else, so retrying another provider there would
        mint a GitHub credential in the name of a repository that is not on
        GitHub. The 404 is the right answer for that pair.
        """
        urlopen.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh", 404, "Not Found", {}, None
        )

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                refresh_git_credentials("owner/repository", provider="gitlab")

        self.assertEqual(1, urlopen.call_count)

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_scopes_token_to_all_managed_repos_in_org(
        self, get_managed_github_repos, urlopen, run
    ):
        import json

        get_managed_github_repos.return_value = [
            "owner/repo1",
            "owner/repo2",
            "other-org/repo3",
        ]

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock()

        run.side_effect = fake_run

        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-installation-token"
        response.__enter__.return_value = response
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("owner", body["org_name"])
        self.assertEqual(["repo1", "repo2"], body["repositories"])
        self.assertEqual("platform-agent-scope", body["scope"])

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_managed_repos_expansion_failure_logs_warning(
        self, get_managed_github_repos, urlopen, run, mock_log
    ):
        import json

        get_managed_github_repos.side_effect = RuntimeError("ConfigMap not found")

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock()

        run.side_effect = fake_run

        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-installation-token"
        response.__enter__.return_value = response
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(["repo1"], body["repositories"])
        mock_log.assert_any_call(
            "WARNING: Could not expand managed repositories for token scoping: ConfigMap not found"
        )

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_sidecar_502(self, urlopen, sleep):
        # The sidecar has already executed retries internally; client fails fast
        err_502 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh",
            502,
            "Bad Gateway",
            email.message.Message(),
            io.BytesIO(b"Bad Gateway"),
        )
        urlopen.side_effect = err_502

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("HTTP 502", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_transport_error(self, urlopen, sleep):
        err_conn = urllib.error.URLError("Connection refused")
        urlopen.side_effect = err_conn

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn(
            "Credential sidecar failed to refresh GitHub auth", str(cm.exception)
        )
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_4xx_without_retry(self, urlopen, sleep):
        err_403 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )
        urlopen.side_effect = err_403

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("HTTP 403", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_general_exception_raises_runtime_error(self, urlopen):
        urlopen.side_effect = TypeError("unexpected type error")

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn(
            "Credential sidecar failed to refresh GitHub auth", str(cm.exception)
        )

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    def test_direct_minty_gcloud_auth_audiences_fallback(self, mock_get_managed, run):
        # First call with --audiences raises, second call without flags succeeds
        res_fail = Exception("gcloud auth print-identity-token --audiences rejected")
        res_ok = MagicMock()
        res_ok.stdout = "fallback-oidc-token\n"
        run.side_effect = [res_fail, res_ok, MagicMock(), MagicMock()]

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"ghs_token_xyz\n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                token = refresh_git_credentials("owner/repository")

            self.assertEqual("ghs_token_xyz", token)

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_gcloud_auth_failure_raises(self, run):
        run.side_effect = [Exception("fail1"), Exception("fail2")]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")
            self.assertIn("Failed to retrieve Google OIDC token", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_empty_oidc_token_raises(self, run):
        res = MagicMock()
        res.stdout = "   \n"
        run.return_value = res
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")
            self.assertIn(
                "Retrieved Google OIDC token via gcloud is empty", str(cm.exception)
            )

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_5xx_and_succeeds(self, urlopen, sleep, mock_get_managed, run):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.side_effect = [run_oidc, MagicMock(), MagicMock()]

        err_500 = urllib.error.HTTPError(
            "http://token-broker",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Internal Error"),
        )
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"ghs_token_12345\n"
        ok_response.__enter__.return_value = ok_response

        urlopen.side_effect = [err_500, ok_response]

        with patch.dict(os.environ, {}, clear=True):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("ghs_token_12345", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_connection_error_and_succeeds(
        self, urlopen, sleep, mock_get_managed, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.side_effect = [run_oidc, MagicMock(), MagicMock()]

        err_conn = urllib.error.URLError("Connection reset by peer")
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"ghs_token_12345\n"
        ok_response.__enter__.return_value = ok_response

        urlopen.side_effect = [err_conn, ok_response]

        with patch.dict(os.environ, {}, clear=True):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("ghs_token_12345", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_immediately_on_403_without_retry(
        self, urlopen, sleep, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        err_403 = urllib.error.HTTPError(
            "http://token-broker",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Repository not allowed"),
        )
        urlopen.side_effect = err_403

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("Repository not allowed", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_after_max_retries_on_persistent_5xx(
        self, urlopen, sleep, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        err_500 = urllib.error.HTTPError(
            "http://token-broker",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Database unavailable"),
        )
        urlopen.side_effect = err_500

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials(
                    "owner/repository",
                    max_attempts=3,
                    initial_delay=0.01,
                    backoff_factor=2.0,
                )

        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([call(0.01), call(0.02)])

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_empty_token_body_raises(self, run):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"   \n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials("owner/repository")
                self.assertIn("Token received from Minty is empty", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_main_cli_execution(self, run):
        with patch.object(sys, "argv", ["github_token_refresh.py", "org/repo"]):
            with patch("github_token_refresh.refresh_git_credentials") as refresh_mock:
                main()
                refresh_mock.assert_called_once_with("org/repo")

    @patch("github_token_refresh.subprocess.run")
    def test_main_cli_execution_failure_exits(self, run):
        with patch.object(sys, "argv", ["github_token_refresh.py", "org/repo"]):
            with patch(
                "github_token_refresh.refresh_git_credentials",
                side_effect=Exception("boom"),
            ):
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(1, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
