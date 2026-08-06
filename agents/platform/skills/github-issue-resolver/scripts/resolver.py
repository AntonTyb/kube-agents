#!/usr/bin/env python3
"""
resolver.py — Deterministic helper script for the github-issue-resolver skill.
Encapsulates GitHub CLI (gh) operations, label management, stale issue sweeps,
and safe report uploading via standard subprocess execution.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys


def get_target_repo() -> str:
    """Extracts target repository from /opt/data/SETTINGS.md."""
    settings_path = "/opt/data/SETTINGS.md"
    if not os.path.exists(settings_path):
        print(f"Error: {settings_path} not found.", file=sys.stderr)
        sys.exit(1)

    with open(settings_path, "r", encoding="utf-8") as f:
        for line in f:
            if "Git Repo:" in line:
                # e.g. "- **Git Repo:** https://github.com/owner/repo.git"
                parts = line.strip().split()
                if parts:
                    repo_url = parts[-1]
                    repo = re.sub(
                        r"^https?://(www\.)?github\.com/", "", repo_url
                    )
                    repo = re.sub(r"\.git$", "", repo)
                    return repo

    print(
        "Error: Could not extract target repository from /opt/data/SETTINGS.md.",
        file=sys.stderr,
    )
    sys.exit(1)


def run_gh(args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Runs a gh CLI command safely without shell escaping or ampersand backgrounding issues."""
    try:
        return subprocess.run(
            ["gh"] + args, check=check, text=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        if check:
            print(
                f"Error running gh command: {' '.join(args)}\n{e.stderr}",
                file=sys.stderr,
            )
            sys.exit(e.returncode)
        return e


def ensure_labels_exist(repo: str):
    """Ensures required status and governance labels exist on the repository."""
    labels = [
        (
            "status:in-progress",
            "FBCA04",
            "Currently being actively investigated by the Platform Agent",
        ),
        (
            "status:resolved",
            "0E8A16",
            "Issue resolved autonomously by Platform Agent",
        ),
        (
            "status:escalation-needed",
            "B60205",
            "Issue requires human review/SRE action",
        ),
        (
            "agent:ignore",
            "E99695",
            "Permanently ignored by automated issue resolvers",
        ),
    ]
    for name, color, desc in labels:
        run_gh(
            [
                "label",
                "create",
                name,
                "-R",
                repo,
                "--color",
                color,
                "--description",
                desc,
                "--force",
            ],
            check=False,
        )


def sweep_stale_issues(repo: str):
    """Detects issues labeled status:in-progress untouched for >2 hours, transitions and alerts."""
    res = run_gh(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--label",
            "status:in-progress",
            "--json",
            "number,title,updatedAt",
        ],
        check=False,
    )
    if res.returncode != 0:
        return

    try:
        issues = json.loads(res.stdout)
        if not isinstance(issues, list):
            issues = []
    except Exception:
        issues = []

    now = datetime.datetime.now(datetime.timezone.utc)
    stale_msg = (
        "🚨 **Autonomous Investigation Timed Out — Human Escalation Required**\n\n"
        "The Platform Agent previously claimed this issue (`status:in-progress`) but no updates were "
        "recorded within the 2-hour SLA window (stale investigation/crash). Transitioning to human review."
    )

    for i in issues:
        updated_str = i.get("updatedAt")
        if not updated_str:
            continue
        try:
            updated = datetime.datetime.fromisoformat(
                updated_str.replace("Z", "+00:00")
            )
            if (now - updated).total_seconds() > 7200:
                num = str(i["number"])
                # Post timeout comment and transition label
                run_gh(
                    [
                        "issue",
                        "comment",
                        num,
                        "-R",
                        repo,
                        "--body",
                        stale_msg,
                    ],
                    check=False,
                )
                run_gh(
                    [
                        "issue",
                        "edit",
                        num,
                        "-R",
                        repo,
                        "--add-label",
                        "status:escalation-needed",
                        "--remove-label",
                        "status:in-progress",
                    ],
                    check=False,
                )
        except Exception:
            continue


def sanitize_untrusted_text(text: str, max_length: int = 8192) -> str:
    """Sanitizes untrusted external input to neutralize prompt injection attacks."""
    if not text or not isinstance(text, str):
        return ""

    # 1. Strip ANSI escape sequences and non-printable control characters
    cleaned = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)
    cleaned = "".join(
        ch for ch in cleaned if ch == "\n" or ch == "\t" or (ord(ch) >= 32 and ord(ch) != 127)
    )

    # 2. Strip Unicode zero-width spaces and direction override characters
    cleaned = re.sub(r"[\u200B-\u200D\uFEFF\u202A-\u202E]", "", cleaned)

    # 3. Neutralize prompt injection delimiter tags and fake system headers
    cleaned = re.sub(
        r"<\s*/?\s*(system|instruction|prompt|context|admin|untrusted_[a-z0-9_-]+)\s*>",
        r"[\1_tag_neutralized]",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"```+\s*(system|instruction|prompt)",
        r"```text",
        cleaned,
        flags=re.IGNORECASE,
    )

    # 4. Truncate to maximum allowable character length with explicit marker
    if len(cleaned) > max_length:
        cleaned = (
            cleaned[:max_length]
            + f"\n\n[TRUNCATED: Exceeded {max_length} character limit]"
        )

    return cleaned.strip()


def calculate_issue_priority(issue: dict) -> tuple[int, str]:
    """Calculates multi-factor priority score and priority label for an issue.
    Returns (score, priority_label).
    """
    labels_raw = issue.get("labels") or []
    label_names = set()
    for l in labels_raw:
        if isinstance(l, dict):
            name = l.get("name", "")
        elif isinstance(l, str):
            name = l
        else:
            name = ""
        if name:
            label_names.add(name.lower())

    score = 0
    priority_label = "UNLABELLED"

    # Priority / Severity weighting
    if any(
        l in label_names
        for l in [
            "priority:critical",
            "priority:p0",
            "severity:critical",
            "blocker",
        ]
    ):
        score += 1000
        priority_label = "P0"
    elif any(
        l in label_names
        for l in ["priority:high", "priority:p1", "severity:high"]
    ):
        score += 500
        if priority_label == "UNLABELLED":
            priority_label = "P1"
    elif any(
        l in label_names for l in ["priority:medium", "priority:p2", "bug"]
    ):
        score += 100
        if priority_label == "UNLABELLED":
            priority_label = "P2"
    elif any(
        l in label_names
        for l in [
            "priority:low",
            "priority:p3",
            "enhancement",
            "documentation",
        ]
    ):
        score += 10
        if priority_label == "UNLABELLED":
            priority_label = "P3"

    return score, priority_label


def evaluate_risk_tier(issue: dict) -> str:
    """Evaluates the risk tier of an issue based on content keywords and labels.
    Returns one of: TIER_1_READ_ONLY, TIER_2_NON_DESTRUCTIVE, TIER_3_MUTATING.
    """
    labels_raw = issue.get("labels") or []
    label_names = set()
    for l in labels_raw:
        if isinstance(l, dict):
            name = l.get("name", "")
        elif isinstance(l, str):
            name = l
        else:
            name = ""
        if name:
            label_names.add(name.lower())

    # Security labels -> Tier 3
    if any(
        l in label_names
        for l in ["security", "security-risk", "privilege-escalation"]
    ):
        return "TIER_3_MUTATING"

    text_parts = [issue.get("title") or "", issue.get("body") or ""]
    for c in issue.get("comments") or []:
        text_parts.append(c.get("body") or "")
    raw_content = " ".join(str(p) for p in text_parts)

    # Strip code blocks and inline snippets to avoid false positives on logs / error messages
    prose_content = re.sub(r"```[\s\S]*?```", "", raw_content)
    prose_content = re.sub(r"`[^`]+`", "", prose_content).lower()

    # Explicit destructive / mutating action verbs or privileged request patterns
    tier3_patterns = [
        r"\b(delete|remove|destroy|drop|kill|drain|truncate|format|overwrite|purge|wipe|cleanup|clean\s+up)\b",
        r"\b(grant\s+admin|escalate\s+privilege|dump\s+secret|export\s+credential)\b",
    ]
    if any(re.search(pat, prose_content) for pat in tier3_patterns):
        return "TIER_3_MUTATING"

    # Check for non-destructive mutations -> Tier 2
    tier2_keywords = [
        "create",
        "add",
        "generate",
        "update",
        "edit",
        "pr",
        "pull request",
        "fix",
        "resolve",
    ]
    if any(
        re.search(r"\b" + re.escape(kw) + r"\b", prose_content)
        for kw in tier2_keywords
    ):
        return "TIER_2_NON_DESTRUCTIVE"

    # Default to read-only diagnostic -> Tier 1
    return "TIER_1_READ_ONLY"


def handle_poll(args):
    repo = get_target_repo()
    # Check auth pre-flight
    run_gh(["auth", "status"])
    # Sweep stale issues first
    sweep_stale_issues(repo)

    # Query next unaddressed issue
    search_query = "is:issue is:open -label:status:in-progress -label:status:escalation-needed -label:agent:ignore -label:status:resolved"
    res = run_gh(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--search",
            search_query,
            "--json",
            "number,title,body,comments,labels,createdAt",
            "--limit",
            "10",
        ]
    )

    try:
        issues = json.loads(res.stdout)
        if not isinstance(issues, list):
            issues = []
    except Exception:
        issues = []

    if not issues:
        print(json.dumps({"status": "NO_ISSUES", "repository": repo}))
        return

    # Select issue by highest priority score, then lowest issue number (FIFO tie-breaker)
    issues.sort(
        key=lambda x: (
            -calculate_issue_priority(x)[0],
            int(x["number"]),
        )
    )
    target = issues[0]
    score, priority_label = calculate_issue_priority(target)
    risk_tier = evaluate_risk_tier(target)

    comments = []
    for c in target.get("comments") or []:
        author = c.get("author", {}).get("login", "unknown")
        body = c.get("body") or ""
        created = c.get("createdAt", "")
        comments.append(
            {
                "author": author,
                "createdAt": created,
                "body": f"<untrusted_comment>{sanitize_untrusted_text(body)}</untrusted_comment>",
            }
        )

    print(
        json.dumps(
            {
                "status": "FOUND",
                "repository": repo,
                "issue_number": target["number"],
                "priority": priority_label,
                "risk_tier": risk_tier,
                "title": f"<untrusted_title>{sanitize_untrusted_text(target.get('title') or '')}</untrusted_title>",
                "body": f"<untrusted_body>{sanitize_untrusted_text(target.get('body') or '')}</untrusted_body>",
                "comments": comments,
            },
            indent=2,
        )
    )


def handle_claim(args):
    repo = get_target_repo()
    issue_num = str(args.issue)
    ensure_labels_exist(repo)

    run_gh(
        [
            "issue",
            "edit",
            issue_num,
            "-R",
            repo,
            "--add-label",
            "status:in-progress",
        ]
    )
    claim_msg = (
        "🤖 **Platform Agent Triaging:** Issue marked `status:in-progress`. "
        "Beginning root cause investigation and recording worklog..."
    )
    run_gh(
        [
            "issue",
            "comment",
            issue_num,
            "-R",
            repo,
            "--body",
            claim_msg,
        ]
    )

    print(
        json.dumps(
            {
                "status": "CLAIMED",
                "issue_number": int(issue_num),
                "repository": repo,
            },
            indent=2,
        )
    )


def handle_transition(args):
    repo = get_target_repo()
    issue_num = str(args.issue)
    state = args.state
    report_file = args.report_file

    if not os.path.exists(report_file):
        print(
            f"Error: Report file {report_file} does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Post report comment directly via file parameter (-F)
    run_gh(["issue", "comment", issue_num, "-R", repo, "-F", report_file])

    # Transition label
    run_gh(
        [
            "issue",
            "edit",
            issue_num,
            "-R",
            repo,
            "--add-label",
            f"status:{state}",
            "--remove-label",
            "status:in-progress",
        ]
    )

    # If resolved, close the issue
    if state == "resolved":
        run_gh(
            [
                "issue",
                "close",
                issue_num,
                "-R",
                repo,
                "--reason",
                "completed",
            ]
        )

    # Cleanup temporary report file
    try:
        os.remove(report_file)
    except Exception:
        pass

    print(
        json.dumps(
            {
                "status": "TRANSITIONED",
                "issue_number": int(issue_num),
                "new_state": state,
                "repository": repo,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic GitHub issue resolver helper."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # poll
    subparsers.add_parser(
        "poll", help="Poll unaddressed issues and sweep stale investigations."
    )

    # claim
    claim_parser = subparsers.add_parser("claim", help="Claim an open issue.")
    claim_parser.add_argument(
        "--issue", required=True, type=int, help="Issue number to claim."
    )

    # transition
    trans_parser = subparsers.add_parser(
        "transition", help="Upload report and transition issue label/state."
    )
    trans_parser.add_argument(
        "--issue", required=True, type=int, help="Issue number to transition."
    )
    trans_parser.add_argument(
        "--state",
        required=True,
        choices=["resolved", "escalation-needed"],
        help="New state label.",
    )
    trans_parser.add_argument(
        "--report-file",
        required=True,
        help="Path to markdown report file to post as comment.",
    )

    args = parser.parse_args()
    if args.subcommand == "poll":
        handle_poll(args)
    elif args.subcommand == "claim":
        handle_claim(args)
    elif args.subcommand == "transition":
        handle_transition(args)


if __name__ == "__main__":
    main()
