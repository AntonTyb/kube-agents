"""Unit tests for code_review.py, the code-review skill's helper.

Run: python3 -m unittest agents/platform/skills/code-review/scripts/test_code_review.py

Four properties carry the weight here, and each one is a thing the persona
cannot be trusted to remember.

The first is that **the agent cannot assent.** ``request-changes`` is a veto and
costs a human an argument; approval is what merges code. The tests below assert
that no argument vector reaches an ``--approve``, rather than asserting that the
help text discourages one.

The second is that **rules are read at the base commit.** A pull request that
can edit the rules governing its own review has not been reviewed, so the test
pins on the sha in the request rather than on the fact that a request was made.

The third is that **a review is idempotent through the marker, not through
local state.** The marker names a commit; the tests check that a matching sha
suppresses, a different one does not, and a missing field fails towards
reviewing rather than towards silence.

The fourth is the one ``test_resolver.py`` also protects: **the body file is
confined to the scratch directory**, and the rejection happens before any
``gh`` call — the body is posted publicly, so an escape is an exfiltration
primitive and printing an error after the post is not a defence.
"""

import ast
import contextlib
import importlib
import inspect
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Import the module under test from this directory.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
cr = importlib.import_module("code_review")

SHA_A = "a" * 40
SHA_B = "b" * 40


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _pr(number=1, **overrides):
    base = {
        "number": number,
        "title": f"pr {number}",
        "author": {"login": "contributor"},
        "headRefName": "feature/thing",
        "headRefOid": SHA_A,
        "baseRefName": "main",
        "isDraft": False,
        "isCrossRepository": False,
        "maintainerCanModify": True,
        "labels": [],
        "latestReviews": [],
        "url": f"https://github.com/o/r/pull/{number}",
    }
    base.update(overrides)
    return base


def _string_literals(module):
    """Every string literal in `module` that is not a docstring.

    A docstring cannot reach `subprocess`; a bare literal can. Separating them
    is what lets the module explain, in prose, the flag it refuses to pass.
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _capture(fn, *args, **kwargs):
    """Run `fn`, returning the JSON it printed to stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return json.loads(buf.getvalue())


# --------------------------------------------------------------------------
# The red line
# --------------------------------------------------------------------------


class NoApprovalTest(unittest.TestCase):
    def test_approve_is_not_an_accepted_verdict(self):
        """argparse is the enforcement, not the prose above it."""
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cr.main(
                ["submit", "--pr", "1", "--head-sha", SHA_A,
                 "--verdict", "approve", "--body-file", "/opt/data/scratch/x.md"]
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_no_string_literal_in_the_module_can_become_an_approve_flag(self):
        """No path, including a future one, can hand `--approve` to `gh`.

        Every argument this script passes to `gh` originates in a string
        literal here — the verdict is mapped through an if/else over two of
        them rather than interpolated. So the absence of the literal is the
        absence of the capability, and this is what would catch someone
        replacing that mapping with a passthrough.

        Docstrings are excluded because the prose *describes* the flag it
        refuses to use, and a test that could not tell those apart would force
        the explanation out of the file to stay green.
        """
        for literal in _string_literals(cr):
            self.assertNotIn("--approve", literal)

    def test_no_string_literal_can_become_a_merge_call(self):
        for literal in _string_literals(cr):
            self.assertNotIn("pr merge", literal)

    def test_the_two_permitted_verdicts_do_parse(self):
        """The counterpart: the guard rejects `approve` and nothing else."""
        for verdict in ("comment", "request-changes"):
            with self.subTest(verdict=verdict), \
                 mock.patch.object(cr, "handle_submit") as handler:
                cr.main(
                    ["submit", "--pr", "1", "--head-sha", SHA_A,
                     "--verdict", verdict, "--body-file", "/opt/data/scratch/x.md"]
                )
                self.assertEqual(handler.call_args[0][0].verdict, verdict)


# --------------------------------------------------------------------------
# poll
# --------------------------------------------------------------------------


class AlreadyReviewedTest(unittest.TestCase):
    def test_a_marker_naming_the_current_head_suppresses(self):
        pr = _pr(latestReviews=[{"body": f"findings\n\n{cr.review_marker(SHA_A)}"}])
        self.assertTrue(cr.already_reviewed(pr))

    def test_a_marker_naming_an_older_commit_does_not(self):
        """A push is new work. This is the whole point of putting a sha in it."""
        pr = _pr(headRefOid=SHA_B, latestReviews=[{"body": cr.review_marker(SHA_A)}])
        self.assertFalse(cr.already_reviewed(pr))

    def test_a_human_review_does_not_suppress(self):
        """We are not the only reviewer, and theirs is not ours."""
        pr = _pr(latestReviews=[{"body": "looks good to me"}])
        self.assertFalse(cr.already_reviewed(pr))

    def test_a_missing_field_fails_towards_reviewing(self):
        """A duplicate review is noise; a silent skip is a PR nobody reads."""
        self.assertFalse(cr.already_reviewed({"headRefOid": SHA_A}))

    def test_a_body_quoting_the_marker_text_without_a_sha_does_not_match(self):
        pr = _pr(latestReviews=[{"body": "<!-- kube-agents:code-review -->"}])
        self.assertFalse(cr.already_reviewed(pr))


class SelectPullRequestTest(unittest.TestCase):
    def test_a_plain_open_pr_is_selected(self):
        self.assertEqual(cr.select_pull_request([_pr(3)], None)["number"], 3)

    def test_drafts_are_skipped(self):
        self.assertIsNone(cr.select_pull_request([_pr(3, isDraft=True)], None))

    def test_the_ignore_label_is_honoured(self):
        pr = _pr(3, labels=[{"name": cr.IGNORE_LABEL}])
        self.assertIsNone(cr.select_pull_request([pr], None))

    def test_our_own_branch_prefix_is_skipped(self):
        """Proposer and reviewer must not be the same actor.

        `submit-suggestion` and `fleet-audit` both cut `platform-agent/…`, so
        the prefix is the harness reviewing its own proposal.
        """
        pr = _pr(3, headRefName="platform-agent/fix-audit-7-abc")
        self.assertIsNone(cr.select_pull_request([pr], None))

    def test_our_own_login_is_skipped_when_configured(self):
        pr = _pr(3, author={"login": "kube-agents-bot"})
        self.assertIsNone(cr.select_pull_request([pr], "kube-agents-bot"))

    def test_the_bot_suffix_is_not_load_bearing_in_that_comparison(self):
        """REST says `name[bot]`, GraphQL says `name`. Neither side is trusted."""
        pr = _pr(3, author={"login": "kube-agents-bot[bot]"})
        self.assertIsNone(cr.select_pull_request([pr], "kube-agents-bot"))

    def test_another_login_is_not_skipped(self):
        pr = _pr(3, author={"login": "someone-else"})
        self.assertIsNotNone(cr.select_pull_request([pr], "kube-agents-bot"))

    def test_a_reviewed_pr_is_skipped_and_the_next_one_taken(self):
        reviewed = _pr(3, latestReviews=[{"body": cr.review_marker(SHA_A)}])
        self.assertEqual(cr.select_pull_request([reviewed, _pr(5)], None)["number"], 5)

    def test_the_lowest_number_wins(self):
        """Stable order, so a backlog drains instead of thrashing."""
        pulls = [_pr(9), _pr(2), _pr(5)]
        self.assertEqual(cr.select_pull_request(pulls, None)["number"], 2)


class PollTest(unittest.TestCase):
    def _run(self, repo="o/r", auth_rc=0, list_rc=0, pulls=None, env=None):
        def fake_gh(args, check=True):
            if args[:2] == ["auth", "status"]:
                return _completed(returncode=auth_rc)
            return _completed(json.dumps(pulls if pulls is not None else []), list_rc)

        with mock.patch.object(cr, "get_target_repo", return_value=repo), \
             mock.patch.object(cr, "run_gh", side_effect=fake_gh), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            if not env:
                os.environ.pop(cr.SELF_LOGIN_ENV, None)
            return _capture(cr.handle_poll, None)

    def test_no_repository_is_not_configured_not_an_error(self):
        """A supported deployment. Flattening it into ERROR pages the room."""
        out = self._run(repo=None)
        self.assertEqual(out["status"], "NOT_CONFIGURED")

    def test_an_unparseable_repository_is_an_error_carrying_the_value(self):
        with mock.patch.object(
            cr, "get_target_repo", side_effect=cr.RepoUnparseable("not a url")
        ):
            out = _capture(cr.handle_poll, None)
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["reason"], "GIT_REPO_UNPARSEABLE")
        self.assertIn("not a url", out["value"])

    def test_a_missing_gh_binary_is_distinguished_from_a_rejected_token(self):
        """Different operators, different fixes, different reason codes."""
        self.assertEqual(self._run(auth_rc=cr.GH_MISSING_RC)["reason"], "GH_CLI_NOT_FOUND")
        self.assertEqual(self._run(auth_rc=1)["reason"], "GITHUB_AUTH_NOT_CONFIGURED")

    def test_an_unreachable_repository_is_an_error(self):
        """`gh auth status` passes per host, so scope failures only show here."""
        out = self._run(list_rc=1)
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["reason"], "REPO_UNREACHABLE")

    def test_an_empty_repository_is_silence(self):
        self.assertEqual(self._run(pulls=[])["status"], "NO_PULL_REQUESTS")

    def test_everything_filtered_out_is_also_silence(self):
        out = self._run(pulls=[_pr(1, isDraft=True), _pr(2, labels=[{"name": "agent:ignore"}])])
        self.assertEqual(out["status"], "NO_PULL_REQUESTS")

    def test_found_reports_the_head_commit(self):
        out = self._run(pulls=[_pr(4)])
        self.assertEqual(out["status"], "FOUND")
        self.assertEqual(out["pr_number"], 4)
        self.assertEqual(out["head_sha"], SHA_A)

    def test_poll_asks_gh_for_no_field_gh_does_not_have(self):
        """`baseRefOid` is not a `gh` field, on `pr list` or on `pr view`.

        Asking for one unknown field fails the whole call, and the failure
        surfaces as `REPO_UNREACHABLE` — which reads as a credential or
        network problem and sends an operator to look in the wrong place. The
        base commit comes from the REST endpoint in `context` instead.
        """
        self.assertNotIn("baseRefOid", cr.PR_LIST_FIELDS)

    def test_a_full_page_is_declared_truncated(self):
        """A silent cap reads as "considered everything" when it did not."""
        pulls = [_pr(n) for n in range(1, cr.PR_LIST_LIMIT + 1)]
        self.assertEqual(self._run(pulls=pulls)["truncated_at"], cr.PR_LIST_LIMIT)

    def test_a_short_page_is_not(self):
        self.assertNotIn("truncated_at", self._run(pulls=[_pr(1)]))

    def test_unparseable_list_output_is_silence_not_a_crash(self):
        def fake_gh(args, check=True):
            if args[:2] == ["auth", "status"]:
                return _completed()
            return _completed("not json")

        with mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             mock.patch.object(cr, "run_gh", side_effect=fake_gh):
            out = _capture(cr.handle_poll, None)
        self.assertEqual(out["status"], "NO_PULL_REQUESTS")


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


class TruncateTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(cr._truncate_text("hello", 100), ("hello", False))

    def test_long_text_is_cut_on_a_line_boundary(self):
        text = "".join(f"line {n}\n" for n in range(100))
        out, cut = cr._truncate_text(text, 50)
        self.assertTrue(cut)
        self.assertTrue(out.endswith("\n"))
        self.assertLessEqual(len(out.encode()), 50)

    def test_a_multibyte_boundary_does_not_raise(self):
        """Slicing bytes can land mid-character; `errors="ignore"` covers it."""
        out, cut = cr._truncate_text("é" * 100, 51)
        self.assertTrue(cut)
        self.assertIsInstance(out, str)


class FetchRulesTest(unittest.TestCase):
    def _api(self, payload, rc=0):
        return mock.patch.object(cr, "run_gh", return_value=_completed(payload, rc))

    def _b64(self, text):
        import base64

        return json.dumps(
            {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        )

    def test_the_read_is_pinned_to_the_base_commit(self):
        """The property this whole argument exists for.

        Read at the head, a pull request could rewrite the standard it is about
        to be measured against. `baseRefOid` is a commit on the target branch,
        which the contributor cannot write.
        """
        with mock.patch.object(
            cr, "run_gh", return_value=_completed(self._b64("no tabs"))
        ) as gh:
            cr.fetch_rules("o/r", SHA_B, ".kube-agents/review.md")
        endpoint = gh.call_args[0][0][1]
        self.assertIn(f"ref={SHA_B}", endpoint)
        self.assertNotIn(SHA_A, endpoint)

    def test_content_is_decoded_and_the_source_reported(self):
        with self._api(self._b64("no tabs")):
            text, source = cr.fetch_rules("o/r", SHA_B, ".kube-agents/review.md")
        self.assertEqual(text, "no tabs")
        self.assertEqual(source, f".kube-agents/review.md@{SHA_B}")

    def test_a_repository_without_rules_is_not_a_fault(self):
        """The common case. Reviewed against the baked procedure alone."""
        with self._api("", rc=1):
            self.assertEqual(cr.fetch_rules("o/r", SHA_B, "x.md"), (None, None))

    def test_an_oversized_file_returning_no_content_is_treated_as_absent(self):
        """GitHub answers `encoding: "none"` with an empty body over ~1MB.

        Guessing at what it held is worse than reporting none.
        """
        with self._api(json.dumps({"encoding": "none", "content": ""})):
            self.assertEqual(cr.fetch_rules("o/r", SHA_B, "x.md"), (None, None))

    def test_oversized_rules_are_cut_and_say_so_in_the_text(self):
        with self._api(self._b64("x" * (cr.RULES_MAX_BYTES + 5000))):
            text, _ = cr.fetch_rules("o/r", SHA_B, "x.md")
        self.assertIn("truncated", text)

    def test_unparseable_json_is_treated_as_absent(self):
        with self._api("<html>404</html>"):
            self.assertEqual(cr.fetch_rules("o/r", SHA_B, "x.md"), (None, None))


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------


class ContextTest(unittest.TestCase):
    """`context` reads the REST representation, which is shaped differently."""

    REST = {
        "number": 4,
        "title": "Add a retry",
        "body": "why",
        "user": {"login": "contributor"},
        "html_url": "https://github.com/o/r/pull/4",
        "head": {"ref": "feature", "sha": SHA_A, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "sha": SHA_B, "repo": {"full_name": "o/r"}},
        "maintainer_can_modify": True,
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
    }

    def _run(self, rest=None, diff="--- a\n+++ b\n", rules=None, scratch=None):
        rest = self.REST if rest is None else rest

        def fake_gh(args, check=True):
            if args[:2] == ["auth", "status"]:
                return _completed()
            if args[0] == "api" and "/pulls/" in args[1]:
                return _completed(json.dumps(rest))
            if args[0] == "api":  # the contents read
                return _completed(rules or "", 0 if rules else 1)
            if args[:2] == ["pr", "diff"]:
                return _completed(diff)
            return _completed()

        with mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             mock.patch.object(cr, "run_gh", side_effect=fake_gh), \
             mock.patch.object(cr, "SCRATCH_DIR", scratch):
            return _capture(cr.handle_context, mock.Mock(pr=4))

    def test_both_commits_come_off_the_rest_object(self):
        with TemporaryDirectory() as scratch:
            out = self._run(scratch=scratch)
        self.assertEqual(out["status"], "READY")
        self.assertEqual(out["head_sha"], SHA_A)
        self.assertEqual(out["base_sha"], SHA_B)

    def test_the_diff_and_metadata_are_written_where_it_says(self):
        with TemporaryDirectory() as scratch:
            out = self._run(scratch=scratch)
            self.assertIn("+++ b", Path(out["diff_path"]).read_text())
            meta = json.loads(Path(out["metadata_path"]).read_text())
        self.assertEqual(meta["author"], "contributor")
        self.assertEqual(meta["changed_files"], 1)

    def test_a_repository_with_no_rules_file_reports_none(self):
        with TemporaryDirectory() as scratch:
            out = self._run(scratch=scratch)
        self.assertIsNone(out["rules_path"])
        self.assertIsNone(out["rules_source"])

    def test_a_same_repo_branch_is_not_a_fork(self):
        with TemporaryDirectory() as scratch:
            self.assertFalse(self._run(scratch=scratch)["is_fork"])

    def test_a_cross_repository_head_is_a_fork(self):
        rest = {**self.REST, "head": {**self.REST["head"], "repo": {"full_name": "them/r"}}}
        with TemporaryDirectory() as scratch:
            self.assertTrue(self._run(rest=rest, scratch=scratch)["is_fork"])

    def test_a_deleted_fork_does_not_crash_the_fork_test(self):
        """`head.repo` is null once the fork it came from is gone."""
        rest = {**self.REST, "head": {"ref": "f", "sha": SHA_A, "repo": None}}
        with TemporaryDirectory() as scratch:
            out = self._run(rest=rest, scratch=scratch)
        self.assertEqual(out["status"], "READY")

    def test_a_pull_request_without_commits_is_an_error_not_a_review(self):
        rest = {**self.REST, "base": {"ref": "main", "sha": "", "repo": {"full_name": "o/r"}}}
        with TemporaryDirectory() as scratch:
            out = self._run(rest=rest, scratch=scratch)
        self.assertEqual(out["reason"], "PR_MISSING_COMMITS")

    def test_an_oversized_diff_is_declared_truncated(self):
        with TemporaryDirectory() as scratch:
            out = self._run(diff="x\n" * cr.DIFF_MAX_BYTES, scratch=scratch)
        self.assertTrue(out["diff_truncated"])
        self.assertEqual(out["diff_limit_bytes"], cr.DIFF_MAX_BYTES)

    def test_a_normal_diff_is_not(self):
        with TemporaryDirectory() as scratch:
            out = self._run(scratch=scratch)
        self.assertFalse(out["diff_truncated"])
        self.assertIsNone(out["diff_limit_bytes"])


class BodyConfinementTest(unittest.TestCase):
    """The body is posted publicly; an escape is an exfiltration primitive."""

    def test_a_path_outside_scratch_is_rejected_before_any_gh_call(self):
        with TemporaryDirectory() as scratch, TemporaryDirectory() as elsewhere:
            secret = os.path.join(elsewhere, "token")
            Path(secret).write_text("s3cret")
            with mock.patch.object(cr, "SCRATCH_DIR", scratch), \
                 mock.patch.object(cr, "run_gh") as gh, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cr._confined_body_file(secret)
            gh.assert_not_called()

    def test_a_symlink_pointing_out_of_scratch_is_rejected(self):
        """`realpath` is what makes the check about the target, not the name."""
        with TemporaryDirectory() as scratch, TemporaryDirectory() as elsewhere:
            secret = os.path.join(elsewhere, "token")
            Path(secret).write_text("s3cret")
            link = os.path.join(scratch, "review.md")
            os.symlink(secret, link)
            with mock.patch.object(cr, "SCRATCH_DIR", scratch), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cr._confined_body_file(link)

    def test_a_traversal_that_lands_back_inside_is_accepted(self):
        """The check is on the resolved path, not on the spelling of it."""
        with TemporaryDirectory() as scratch:
            os.mkdir(os.path.join(scratch, "sub"))
            real = os.path.join(scratch, "review.md")
            Path(real).write_text("body")
            with mock.patch.object(cr, "SCRATCH_DIR", scratch):
                got = cr._confined_body_file(os.path.join(scratch, "sub", "..", "review.md"))
            self.assertEqual(str(got), os.path.realpath(real))

    def test_the_scratch_directory_itself_is_not_a_body(self):
        """`startswith(scratch)` without the separator would also accept
        `/opt/data/scratch-evil/token`. The separator is why it does not."""
        with TemporaryDirectory() as parent:
            scratch = os.path.join(parent, "scratch")
            os.mkdir(scratch)
            sibling = os.path.join(parent, "scratch-evil")
            os.mkdir(sibling)
            secret = os.path.join(sibling, "token")
            Path(secret).write_text("s3cret")
            with mock.patch.object(cr, "SCRATCH_DIR", scratch), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cr._confined_body_file(secret)


class SubmitTest(unittest.TestCase):
    def _args(self, scratch, body_file, **overrides):
        ns = mock.Mock()
        ns.pr = 7
        ns.head_sha = SHA_A
        ns.verdict = "comment"
        ns.body_file = body_file
        ns.cleanup = False
        ns.dry_run = False
        for key, value in overrides.items():
            setattr(ns, key, value)
        return ns

    @contextlib.contextmanager
    def _scratch(self, body="findings here"):
        with TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "review.md")
            Path(path).write_text(body)
            with mock.patch.object(cr, "SCRATCH_DIR", scratch):
                yield scratch, path

    def _gh(self, live_sha=SHA_A, review_rc=0, calls=None):
        def fake_gh(args, check=True):
            if args[:2] == ["auth", "status"]:
                return _completed()
            if args[0] == "pr" and args[1] == "view":
                return _completed(json.dumps({"headRefOid": live_sha}))
            if args[0] == "pr" and args[1] == "review":
                if calls is not None:
                    calls.append(args)
                return _completed(returncode=review_rc, stderr="denied" if review_rc else "")
            return _completed()

        return mock.patch.object(cr, "run_gh", side_effect=fake_gh)

    def test_a_moved_branch_is_stale_and_nothing_is_posted(self):
        """The review describes code that no longer exists.

        Posting it would be worse than not reviewing: it looks current.
        """
        calls = []
        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(live_sha=SHA_B, calls=calls):
            out = _capture(cr.handle_submit, self._args(_, path))
        self.assertEqual(out["status"], "STALE")
        self.assertEqual(out["current_sha"], SHA_B)
        self.assertEqual(calls, [])

    def test_a_successful_post_stamps_the_marker(self):
        """Appended here, not asked of the model.

        An idempotency guarantee that depends on the model remembering to write
        a comment is not a guarantee.
        """
        calls = []
        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(calls=calls):
            out = _capture(cr.handle_submit, self._args(_, path))
            # Inside the scratch context, where a leftover file would still be
            # on disk: the stamped copy is unlinked after a successful post so
            # it cannot be mistaken for a pending review on the next run.
            self.assertFalse(Path(calls[0][-1]).exists())
        self.assertEqual(out["status"], "SUBMITTED")
        self.assertIn("--comment", calls[0])

    def test_the_marker_is_in_the_body_that_reaches_gh(self):
        captured = {}

        def fake_gh(args, check=True):
            if args[:2] == ["auth", "status"]:
                return _completed()
            if args[1] == "view":
                return _completed(json.dumps({"headRefOid": SHA_A}))
            captured["body"] = Path(args[-1]).read_text()
            return _completed()

        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             mock.patch.object(cr, "run_gh", side_effect=fake_gh):
            _capture(cr.handle_submit, self._args(_, path))
        self.assertIn(cr.review_marker(SHA_A), captured["body"])
        self.assertIn("findings here", captured["body"])

    def test_request_changes_maps_to_the_veto_flag(self):
        calls = []
        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(calls=calls):
            _capture(cr.handle_submit, self._args(_, path, verdict="request-changes"))
        self.assertIn("--request-changes", calls[0])
        self.assertNotIn("--comment", calls[0])

    def test_an_empty_body_is_refused(self):
        with self._scratch(body="   \n") as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cr.handle_submit(self._args(_, path))

    def test_a_dry_run_posts_nothing_but_renders_the_body(self):
        calls = []
        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(calls=calls):
            out = _capture(cr.handle_submit, self._args(_, path, dry_run=True))
            # Asserted inside the scratch context: the rendered file lives in
            # the TemporaryDirectory, which is gone by the time the `with` exits.
            self.assertIn(cr.review_marker(SHA_A), Path(out["body_path"]).read_text())
        self.assertEqual(out["status"], "DRY_RUN")
        self.assertEqual(calls, [])

    def test_a_rejected_review_reports_the_detail(self):
        with self._scratch() as (_, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(review_rc=1):
            out = _capture(cr.handle_submit, self._args(_, path))
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["reason"], "REVIEW_REJECTED")
        self.assertIn("denied", out["detail"])

    def test_the_context_survives_a_failed_post(self):
        """It is exactly what a retry needs, so `--cleanup` must not run first."""
        with self._scratch() as (scratch, path), \
             mock.patch.object(cr, "get_target_repo", return_value="o/r"), \
             self._gh(review_rc=1):
            _capture(cr.handle_submit, self._args(scratch, path, cleanup=True))
            self.assertTrue(Path(path).exists())


class SlugTest(unittest.TestCase):
    def test_a_repository_slug_loses_its_separator(self):
        self.assertEqual(cr._slug("gke-labs/kube-agents"), "gke-labs-kube-agents")

    def test_an_unrepresentable_value_still_yields_a_path_component(self):
        self.assertEqual(cr._slug("///"), "unknown")


if __name__ == "__main__":
    unittest.main()
