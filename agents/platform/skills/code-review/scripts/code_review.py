#!/usr/bin/env python3
"""
code_review.py — Deterministic helper script for the code-review skill.

Encapsulates every GitHub CLI (gh) operation the skill needs: finding the next
pull request that wants a review, collecting the review context, and posting
the finished review. The LLM's role is strictly the part that cannot be
written down — reading the diff and deciding what is wrong with it.

Three properties are enforced here rather than left to the persona, because a
persona is advice and this is a contract:

**The agent can never assent.** ``--verdict`` accepts ``comment`` and
``request-changes`` and nothing else. There is no passthrough of arbitrary
``gh`` arguments, so ``--approve`` cannot be reached from this script even by a
model that decides it would like to. Gatekeepers veto, approvers assent, and an
agent never assents: a veto is monotone in the safe direction — its worst case
is a human's time — while an approval is what merges code. That asymmetry is
why ``request-changes`` is offered and approval is not.

Note what this does and does not buy. The credential broker permits
``gh pr review --approve`` today, so the guarantee holds for work that goes
*through this script* and is not a barrier against a model that shells out to
``gh`` on its own. ``SKILL.md`` therefore states the rule as a red line as well,
and a broker rule denying assent outright is the missing third layer.

**The durable claim lives on GitHub, not in local state.** Every review this
script posts carries ``REVIEW_MARKER`` naming the exact commit it reviewed, and
``poll`` skips any pull request whose current head already has one. A reset
volume, a re-imaged pod or a second replica therefore cannot produce a
duplicate review — the same reasoning that put ``status:in-progress`` on the
issue rather than in a file (see ``github_scan_gate.py``).

**Repository-supplied rules are read at the base, never at the head.** A pull
request that could rewrite the rules governing its own review has not been
reviewed. ``fetch_rules`` pins the read to ``baseRefOid``, which is a commit on
the target branch and is not reachable by anything the contributor pushed.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# The sibling skill owns the hardened SETTINGS.md parser — the one that rejects
# `https://evil.com/github.com/attacker/repo`, where a looser reading of the
# same line resolves to `attacker/repo`. Importing it is deliberate: a third
# copy of repository resolution in this repository would be a third parser to
# keep in step, and the two that already exist do not agree. The layout under
# `$HERMES_HOME/skills/` is identical to the layout in the source tree, so this
# one line works in both without consulting the environment.
sys.path.append(str(Path(__file__).resolve().parents[2] / "github-issue-resolver" / "scripts"))

from resolver import (  # noqa: E402  (path must be set before the import)
    GH_MISSING_RC,
    RepoUnparseable,
    get_target_repo,
    run_gh,
)

# Where review context and review bodies are written. Shared and unversioned —
# `fleet-audit` says the same thing about it — so every path this script builds
# is namespaced by repository, pull request and commit, and every path it is
# *handed* is confined to this directory before it is read.
SCRATCH_DIR = "/opt/data/scratch"

# Stamped into the body of every review this script posts, and the only thing
# that makes a review idempotent. `poll` reads it back off `latestReviews` and
# skips the pull request when the sha matches its current head.
#
# The full 40-character sha, not an abbreviation: this string is compared for
# equality against `headRefOid`, and an abbreviation would make the comparison
# depend on how many characters each side happened to choose.
REVIEW_MARKER_RE = re.compile(r"<!--\s*kube-agents:code-review\s+sha=([0-9a-f]{40})\s*-->")


def review_marker(head_sha: str) -> str:
    return f"<!-- kube-agents:code-review sha={head_sha} -->"


# The repository's own review rules, read as DATA. Overridable because a
# repository that already keeps its conventions in `AGENTS.md` or
# `CONTRIBUTING.md` should not have to duplicate them to be reviewed.
RULES_PATH_ENV = "CODE_REVIEW_RULES_PATH"
DEFAULT_RULES_PATH = ".kube-agents/review.md"

# Caps. Both are stated in the metadata when they bite and the skill is required
# to repeat them in the review — a review that silently saw half the diff reads
# exactly like a review that found nothing wrong with the other half.
RULES_MAX_BYTES = 64 * 1024
DIFF_MAX_BYTES = 512 * 1024

# How many open pull requests `poll` considers on one tick.
PR_LIST_LIMIT = 50

# Branch prefixes the harness itself pushes: `submit-suggestion` cuts
# `platform-agent/<change_type>-<target_id>` and `fleet-audit` cuts
# `platform-agent/fix-<audit-id>-…`. A pull request on one of these is the
# agent's own proposal, and an agent that reviews its own proposal has collapsed
# proposer and reviewer into one actor for a second time.
#
# A heuristic, and named as one: it holds because both write paths in this
# repository share the prefix, and it fails open (towards reviewing) rather than
# closed. `CODE_REVIEW_SELF_LOGIN` below is the exact check for an install that
# wants one.
AGENT_BRANCH_PREFIXES = ("platform-agent/",)

# Exact self-exclusion, when the deployment knows its own App login. Optional
# because it cannot be derived: `gh api user` has no user to report under an
# App installation token, which is what this agent authenticates with.
SELF_LOGIN_ENV = "CODE_REVIEW_SELF_LOGIN"

# A pull request carrying this label is not reviewed. Same spelling and same
# meaning as the issue resolver's, so one label opts a repository's work out of
# every autonomous surface rather than one per skill.
IGNORE_LABEL = "agent:ignore"

# The fields one `gh pr list` call has to return for `poll` to decide without a
# second round trip. `latestReviews` carries the review bodies — hence the
# marker check costs nothing extra — and `isCrossRepository` /
# `maintainerCanModify` are collected because a fork pull request is one the
# later write phases cannot push to, and the skill should be able to say so.
#
# No base commit here, and none in `gh pr view` either: `baseRefName` is the
# only base field `gh` exposes on a pull request, so the sha the rules read has
# to be pinned to comes from the REST endpoint in `handle_context` instead.
# `poll` does not need it — it decides on the head — so this stays one call.
PR_LIST_FIELDS = (
    "number,title,author,headRefName,headRefOid,baseRefName,"
    "isDraft,isCrossRepository,maintainerCanModify,labels,latestReviews,url,updatedAt"
)


def _fail(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


def _slug(text: str) -> str:
    """Reduce a repository slug or branch name to something safe in a path."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-").lower() or "unknown"


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------
# Preflight, shared by every subcommand
# --------------------------------------------------------------------------


def resolve_repo_or_status() -> tuple[Optional[str], Optional[dict]]:
    """``(repo, None)`` when a repository is configured and parseable.

    Otherwise ``(None, status)``, where ``status`` is the JSON object the caller
    should print. The two failure modes stay apart for the reason the resolver
    keeps them apart: nothing configured is a supported deployment and must be
    silent, while something configured that cannot be read is a fault that has
    to reach a human or the watcher goes quiet forever.
    """
    try:
        repo = get_target_repo(required=False)
    except RepoUnparseable as e:
        return None, {"status": "ERROR", "reason": "GIT_REPO_UNPARSEABLE", "value": str(e)}
    if not repo:
        return None, {"status": "NOT_CONFIGURED"}
    return repo, None


def gh_auth_status() -> Optional[dict]:
    """``None`` when ``gh`` can authenticate, else the ERROR object to print.

    An absent binary and a rejected token need different operators and different
    fixes, so they do not share a reason code.
    """
    auth = run_gh(["auth", "status"], check=False)
    if auth.returncode == 0:
        return None
    reason = (
        "GH_CLI_NOT_FOUND"
        if auth.returncode == GH_MISSING_RC
        else "GITHUB_AUTH_NOT_CONFIGURED"
    )
    return {"status": "ERROR", "reason": reason}


# --------------------------------------------------------------------------
# poll
# --------------------------------------------------------------------------


def already_reviewed(pr: dict) -> bool:
    """True when this exact head commit already carries one of our reviews.

    Reads `latestReviews`, which GitHub defines as the most recent non-pending
    review per author — so our own previous review is in there whether or not
    humans have reviewed since.

    Absence of the field is treated as "not reviewed" rather than as an error.
    That fails towards reviewing: a duplicate review is noise, while a silent
    "already reviewed" on a field that did not arrive is a pull request nobody
    ever looks at.
    """
    for review in pr.get("latestReviews") or []:
        match = REVIEW_MARKER_RE.search(review.get("body") or "")
        if match and match.group(1) == pr.get("headRefOid"):
            return True
    return False


def is_agent_authored(pr: dict, self_login: Optional[str]) -> bool:
    """True when the harness itself opened this pull request."""
    if self_login:
        author = ((pr.get("author") or {}).get("login") or "").lower()
        # GraphQL and REST disagree about the `[bot]` suffix on an App's login,
        # so neither side of this comparison is trusted to carry it.
        if author.removesuffix("[bot]") == self_login.lower().removesuffix("[bot]"):
            return True
    head = pr.get("headRefName") or ""
    return head.startswith(AGENT_BRANCH_PREFIXES)


def is_ignored(pr: dict) -> bool:
    return any(
        (label.get("name") or "") == IGNORE_LABEL for label in (pr.get("labels") or [])
    )


def select_pull_request(pulls: list, self_login: Optional[str]) -> Optional[dict]:
    """The next pull request wanting a review, or None.

    Lowest number first, matching the issue resolver: an arbitrary but stable
    order means a repository with a backlog drains it rather than thrashing
    between two candidates on consecutive ticks.
    """
    candidates = [
        pr
        for pr in pulls
        if not pr.get("isDraft")
        and not is_ignored(pr)
        and not is_agent_authored(pr, self_login)
        and not already_reviewed(pr)
    ]
    candidates.sort(key=lambda pr: int(pr["number"]))
    return candidates[0] if candidates else None


def handle_poll(args) -> None:
    repo, status = resolve_repo_or_status()
    if status:
        _emit(status)
        return

    auth_error = gh_auth_status()
    if auth_error:
        _emit(auth_error)
        return

    res = run_gh(
        [
            "pr", "list",
            "-R", repo,
            "--state", "open",
            "--limit", str(PR_LIST_LIMIT),
            "--json", PR_LIST_FIELDS,
        ],
        check=False,
    )
    if res.returncode != 0:
        # `gh auth status` passes when *any* host is authenticated, so a token
        # without scope for this repository — or a repository that 404s — only
        # fails here.
        _emit({"status": "ERROR", "reason": "REPO_UNREACHABLE", "repository": repo})
        return

    try:
        pulls = json.loads(res.stdout)
        if not isinstance(pulls, list):
            pulls = []
    except Exception:
        pulls = []

    target = select_pull_request(pulls, os.environ.get(SELF_LOGIN_ENV, "").strip() or None)
    if target is None:
        _emit({"status": "NO_PULL_REQUESTS", "repository": repo})
        return

    payload = {
        "status": "FOUND",
        "repository": repo,
        "pr_number": target["number"],
        "title": target.get("title", ""),
        "url": target.get("url", ""),
        "author": (target.get("author") or {}).get("login", "unknown"),
        "head_ref": target.get("headRefName", ""),
        "head_sha": target.get("headRefOid", ""),
        "base_ref": target.get("baseRefName", ""),
        "is_fork": bool(target.get("isCrossRepository")),
        "maintainer_can_modify": bool(target.get("maintainerCanModify")),
    }
    # Said out loud rather than left for a reader to notice a number that stopped
    # moving: a repository with more open pull requests than one page cannot be
    # promised to have been considered in full.
    if len(pulls) >= PR_LIST_LIMIT:
        payload["truncated_at"] = PR_LIST_LIMIT
    _emit(payload)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit` bytes on a line boundary. Returns (text, cut?)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    newline = clipped.rfind("\n")
    return (clipped[: newline + 1] if newline > 0 else clipped), True


def fetch_rules(repo: str, base_sha: str, rules_path: str) -> tuple[Optional[str], Optional[str]]:
    """The repository's review rules at `base_sha`, and where they came from.

    ``(None, None)`` when the repository ships none — the common case, and not a
    fault. A repository with no rules file is reviewed against the baked
    procedure alone.

    Pinned to `base_sha` on purpose. `base_sha` is `baseRefOid`, the tip of the
    branch the pull request targets, so it is a commit the contributor cannot
    write. Reading the rules at the *head* would let a pull request edit the
    standard it is about to be measured against, which is the whole attack this
    argument closes.
    """
    # The ref is a query parameter in the path rather than `-X GET -f ref=…`:
    # both reach the same endpoint, but the explicit `-X` reads like a mutation
    # to a regex policy engine, and this call is a read.
    endpoint = (
        f"repos/{repo}/contents/{quote(rules_path)}?ref={quote(base_sha, safe='')}"
    )
    res = run_gh(["api", endpoint], check=False)
    if res.returncode != 0:
        return None, None
    try:
        document = json.loads(res.stdout)
    except Exception:
        return None, None
    if not isinstance(document, dict) or document.get("encoding") != "base64":
        # A file over GitHub's inline-content ceiling comes back with an empty
        # body and `encoding: "none"`. A rules file that large is a
        # misconfiguration, and guessing at it is worse than reporting none.
        return None, None
    try:
        content = base64.b64decode(document.get("content") or "").decode("utf-8", errors="replace")
    except Exception:
        return None, None
    text, cut = _truncate_text(content, RULES_MAX_BYTES)
    if cut:
        text += f"\n\n[truncated at {RULES_MAX_BYTES} bytes by code_review.py]\n"
    return text, f"{rules_path}@{base_sha}"


def review_dir(repo: str, pr_number: int, head_sha: str) -> Path:
    return Path(SCRATCH_DIR) / f"review_{_slug(repo)}_{pr_number}_{head_sha[:7]}"


def handle_context(args) -> None:
    repo, status = resolve_repo_or_status()
    if status:
        _emit(status)
        return
    auth_error = gh_auth_status()
    if auth_error:
        _emit(auth_error)
        return

    number = str(args.pr)
    # The REST representation rather than `gh pr view --json`, because it is the
    # only one of the two that carries `base.sha`. `gh` exposes `baseRefName`
    # and no base commit at all, and a branch name is not what the rules read
    # can be pinned to: it names whatever the tip is at the moment of the
    # request, so two runs of this command could legitimately disagree about
    # which rules governed the review. Everything else `context` needs happens
    # to be on the same object, so this is still one call.
    res = run_gh(["api", f"repos/{repo}/pulls/{quote(number, safe='')}"], check=False)
    if res.returncode != 0:
        _emit({"status": "ERROR", "reason": "PR_UNREACHABLE", "repository": repo, "pr_number": args.pr})
        return
    try:
        pr = json.loads(res.stdout)
    except Exception:
        _emit({"status": "ERROR", "reason": "PR_UNPARSEABLE", "repository": repo, "pr_number": args.pr})
        return

    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_sha = head.get("sha") or ""
    base_sha = base.get("sha") or ""
    if not head_sha or not base_sha:
        _emit({"status": "ERROR", "reason": "PR_MISSING_COMMITS", "repository": repo, "pr_number": args.pr})
        return

    diff_res = run_gh(["pr", "diff", number, "-R", repo], check=False)
    if diff_res.returncode != 0:
        _emit({"status": "ERROR", "reason": "DIFF_UNAVAILABLE", "repository": repo, "pr_number": args.pr})
        return
    diff, diff_cut = _truncate_text(diff_res.stdout, DIFF_MAX_BYTES)

    rules_path = os.environ.get(RULES_PATH_ENV, "").strip() or DEFAULT_RULES_PATH
    rules, rules_source = fetch_rules(repo, base_sha, rules_path)

    target = review_dir(repo, int(number), head_sha)
    target.mkdir(parents=True, exist_ok=True)
    (target / "diff.patch").write_text(diff, encoding="utf-8")
    if rules is not None:
        (target / "rules.md").write_text(rules, encoding="utf-8")
    # `head.repo` is null when the fork it came from has been deleted, so the
    # fork test is on the repository full names rather than on `head.repo.fork`.
    head_repo = ((head.get("repo") or {}).get("full_name") or "").lower()
    base_repo = ((base.get("repo") or {}).get("full_name") or "").lower()
    is_fork = bool(head_repo) and head_repo != base_repo
    metadata = {
        "repository": repo,
        "pr_number": pr.get("number"),
        "title": pr.get("title", ""),
        "url": pr.get("html_url", ""),
        "author": (pr.get("user") or {}).get("login", "unknown"),
        "head_ref": head.get("ref", ""),
        "head_sha": head_sha,
        "base_ref": base.get("ref", ""),
        "base_sha": base_sha,
        "is_fork": is_fork,
        "maintainer_can_modify": bool(pr.get("maintainer_can_modify")),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "description": pr.get("body") or "",
    }
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    _emit(
        {
            "status": "READY",
            "repository": repo,
            "pr_number": int(number),
            "head_sha": head_sha,
            "base_sha": base_sha,
            "context_dir": str(target),
            "diff_path": str(target / "diff.patch"),
            "metadata_path": str(target / "metadata.json"),
            "rules_path": str(target / "rules.md") if rules is not None else None,
            "rules_source": rules_source,
            "diff_truncated": diff_cut,
            "diff_limit_bytes": DIFF_MAX_BYTES if diff_cut else None,
            "is_fork": is_fork,
        }
    )


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------


def _confined_body_file(body_file: str) -> Path:
    """`body_file` as a real path inside SCRATCH_DIR, or exit.

    The review body is posted publicly, so anything resolving outside the
    scratch directory — including via a symlink — is rejected rather than read.
    Same rule, and the same reason, as `resolver.py transition`.
    """
    scratch = os.path.realpath(SCRATCH_DIR)
    real = os.path.realpath(body_file)
    if not real.startswith(scratch + os.sep):
        _fail(f"Review body {body_file} resolves outside {scratch}.")
    if not os.path.exists(real):
        _fail(f"Review body {body_file} does not exist.")
    return Path(real)


def current_head_sha(repo: str, number: str) -> Optional[str]:
    res = run_gh(["pr", "view", number, "-R", repo, "--json", "headRefOid"], check=False)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout).get("headRefOid")
    except Exception:
        return None


def handle_submit(args) -> None:
    repo, status = resolve_repo_or_status()
    if status:
        _emit(status)
        return
    auth_error = gh_auth_status()
    if auth_error:
        _emit(auth_error)
        return

    number = str(args.pr)
    body_path = _confined_body_file(args.body_file)

    # The branch may have moved while the model was reading the diff. Posting
    # then would attach findings about code that is no longer there — worse than
    # not reviewing, because the review looks current and is not.
    live_sha = current_head_sha(repo, number)
    if live_sha is None:
        _emit({"status": "ERROR", "reason": "PR_UNREACHABLE", "repository": repo, "pr_number": args.pr})
        return
    if live_sha != args.head_sha:
        _emit(
            {
                "status": "STALE",
                "repository": repo,
                "pr_number": int(number),
                "reviewed_sha": args.head_sha,
                "current_sha": live_sha,
            }
        )
        return

    body = body_path.read_text(encoding="utf-8").rstrip()
    if not body:
        _fail("Review body is empty; refusing to post an empty review.")
    # The marker goes last so a human reading the review sees the findings
    # first, and it is appended here rather than asked of the model because an
    # idempotency guarantee that depends on the model remembering to write a
    # comment is not a guarantee.
    stamped = f"{body}\n\n{review_marker(args.head_sha)}\n"
    stamped_path = body_path.with_name(body_path.name + ".posted")
    stamped_path.write_text(stamped, encoding="utf-8")

    # `--comment` and `--request-changes` only. `--approve` is unreachable from
    # here by construction (argparse `choices`), and is separately denied at the
    # credential broker.
    verdict_flag = "--comment" if args.verdict == "comment" else "--request-changes"

    if args.dry_run:
        _emit(
            {
                "status": "DRY_RUN",
                "repository": repo,
                "pr_number": int(number),
                "head_sha": args.head_sha,
                "verdict": args.verdict,
                "body_path": str(stamped_path),
                "bytes": len(stamped.encode("utf-8")),
            }
        )
        return

    res = run_gh(
        ["pr", "review", number, "-R", repo, verdict_flag, "--body-file", str(stamped_path)],
        check=False,
    )
    try:
        stamped_path.unlink()
    except OSError:
        pass
    if res.returncode != 0:
        _emit(
            {
                "status": "ERROR",
                "reason": "REVIEW_REJECTED",
                "repository": repo,
                "pr_number": int(number),
                "detail": (res.stderr or "").strip()[:500],
            }
        )
        return

    # Only now is the context safe to drop: it is what a retry would have needed.
    if args.cleanup:
        shutil.rmtree(review_dir(repo, int(number), args.head_sha), ignore_errors=True)

    _emit(
        {
            "status": "SUBMITTED",
            "repository": repo,
            "pr_number": int(number),
            "head_sha": args.head_sha,
            "verdict": args.verdict,
        }
    )


# --------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Deterministic code-review helper.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("poll", help="Find the next pull request wanting a review.")

    context_parser = subparsers.add_parser(
        "context", help="Collect diff, metadata and repository rules for one pull request."
    )
    context_parser.add_argument("--pr", required=True, type=int, help="Pull request number.")

    submit_parser = subparsers.add_parser("submit", help="Post the finished review.")
    submit_parser.add_argument("--pr", required=True, type=int, help="Pull request number.")
    submit_parser.add_argument(
        "--head-sha",
        required=True,
        help="The commit the review was written against. Refused if the branch has moved.",
    )
    submit_parser.add_argument(
        "--verdict",
        required=True,
        choices=["comment", "request-changes"],
        help="Approval is deliberately not offered; an agent never assents.",
    )
    submit_parser.add_argument(
        "--body-file", required=True, help=f"Markdown review body, inside {SCRATCH_DIR}."
    )
    submit_parser.add_argument(
        "--cleanup", action="store_true", help="Remove the context directory after a successful post."
    )
    submit_parser.add_argument(
        "--dry-run", action="store_true", help="Render the review without posting it."
    )

    args = parser.parse_args(argv)
    if args.subcommand == "poll":
        handle_poll(args)
    elif args.subcommand == "context":
        handle_context(args)
    elif args.subcommand == "submit":
        handle_submit(args)


if __name__ == "__main__":
    main()
