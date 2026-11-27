"""Optional enhancement: collect pull-request evidence via the GitHub CLI.

This is strictly additive. If ``gh`` is absent, unauthenticated, or offline, collection
returns an empty result and the pack is still built from git alone. The tool never
*requires* the network (spec §1.4: evidence comes from the customer's named systems;
git is the primary one).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from kerbtap.honesty import neutralize_barred, redact_quoted
from kerbtap.model import PullRequest
from kerbtap.textutil import clean_excerpt

logger = logging.getLogger(__name__)

# Fields fetched per PR to make each manifest record standalone (no re-query needed).
_PR_VIEW_FIELDS = (
    "number,title,state,body,mergedAt,mergedBy,mergeCommit,reviews,author,baseRefName,headRefName,headRefOid"
)

# Check-run conclusions, as GitHub's REST API defines them for a check run.
_CHECK_PASS = "success"
_CHECK_NEUTRAL = frozenset({"neutral", "skipped"})
_CHECK_FAIL = frozenset({"failure", "timed_out"})

# Fixed wording for every check-run outcome. Never built from repo-sourced text.
CHECKS_LIMITS = (
    "Covers check runs on the pull request's head commit only: commit statuses and checks on "
    "GitHub's test-merge commit are not read. The outcome counts runs from every publisher: "
    "each run's publishing App is recorded for the reader to judge, and no App is treated as "
    "required. The pack does not show that the checks were "
    "required or that the workflow was unchanged. The base can move after the checks ran, so "
    "the merged tree can differ from the tested tree. Each check shows its latest run at "
    "collection time, so a failure that a rerun replaced does not show."
)
_CHECKS_BASIS = {
    "pass": ("Every check run on the head commit succeeded, was neutral or was skipped, and at least "
             "one succeeded."),
    "fail": "At least one check run on the head commit failed or timed out.",
    "no-recorded-evidence": "No check run was recorded on the pull request's head commit.",
    "undetermined": ("The check runs on the head commit did not settle to a pass or a fail "
                     "(a run was incomplete, cancelled, stale, waiting for action, or reported an "
                     "unrecognized conclusion, or no run succeeded)."),
    "unreadable": "The check runs on the head commit could not be read this run.",
    "truncated": "The head commit has more check runs than one page returns, so the list is incomplete.",
    "no-head": "The pull request's head commit was not reported, so its check runs were not read.",
}
CHECKS_NOT_COLLECTED = {
    "outcome": "undetermined",
    "basis": "Pull-request details were not collected, so its check runs were not read.",
}


def classify_check_runs(runs: list[dict]) -> str:
    """Outcome for one head commit's check runs; the first matching rule wins."""
    if not runs:
        return "no-recorded-evidence"
    conclusions = [r.get("conclusion") if r.get("status") == "completed" else None for r in runs]
    if any(c in _CHECK_FAIL for c in conclusions):
        return "fail"
    if _CHECK_PASS in conclusions and all(c == _CHECK_PASS or c in _CHECK_NEUTRAL for c in conclusions):
        return "pass"
    return "undetermined"


def _checks_block(repo_path: Path, head_sha: str | None) -> dict[str, object]:
    """The ``checks`` record for one PR: per-run publisher, outcome and fixed-wording basis.

    Records each run's publishing App, which a combined rollup does not name. The outcome
    counts runs from every publisher: no App is treated as required.
    """
    if not head_sha:
        return {"outcome": "undetermined", "basis": _CHECKS_BASIS["no-head"]}
    payload, _err = _gh_api(repo_path, f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs?filter=latest&per_page=100")
    raw = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return {"head_sha": head_sha, "outcome": "undetermined", "basis": _CHECKS_BASIS["unreadable"]}
    runs = [
        {
            "name": redact_quoted(str(r.get("name") or ""))[0],
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "app": _safe_login((r.get("app") or {}).get("slug") if isinstance(r.get("app"), dict) else None),
        }
        for r in raw
        if isinstance(r, dict)
    ]
    total = payload.get("total_count")
    if isinstance(total, int) and total > len(runs):
        return {"head_sha": head_sha, "outcome": "undetermined", "basis": _CHECKS_BASIS["truncated"], "runs": runs}
    outcome = classify_check_runs(runs)
    return {
        "head_sha": head_sha,
        "outcome": outcome,
        "basis": _CHECKS_BASIS[outcome],
        "limits": CHECKS_LIMITS,
        "runs": runs,
        "collection": "gh api: commits/{sha}/check-runs (publisher read per run)",
    }


def gh_available() -> bool:
    """Return whether an authenticated ``gh`` CLI is usable."""
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def repo_resolves(repo_path: Path) -> bool:
    """Return whether ``gh`` resolves a GitHub repository for the checkout at ``repo_path``.

    False for a checkout with no remote, a non-GitHub remote, or any ``gh`` failure.
    """
    try:
        subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_path),
            timeout=30,
        )
    except FileNotFoundError:
        logger.info("gh CLI not found; no GitHub repository resolved")
        return False
    except subprocess.CalledProcessError as exc:
        logger.info("no GitHub repository resolves for this checkout (gh exit code %s)", exc.returncode)
        return False
    except subprocess.TimeoutExpired as exc:
        logger.warning("gh repo view timed out; treating as no GitHub repository: %s", exc)
        return False
    return True


def collect_pull_requests(repo_path: Path, limit: int = 200) -> list[PullRequest]:
    """Collect merged/all PRs for the repo, or an empty list if gh is unavailable."""
    if not gh_available():
        logger.info("gh CLI unavailable or unauthenticated; building git-only")
        return []
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--state", "all",
                "--limit", str(limit),
                "--json", "number,title,body,state,mergedAt,headRefName",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            cwd=str(repo_path),
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("gh pr list failed; degrading to git-only: %s", exc)
        return []

    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("gh returned unparseable JSON; degrading to git-only")
        return []

    prs = [
        PullRequest(
            number=int(item["number"]),
            title=item.get("title", ""),
            body=item.get("body", "") or "",
            state=item.get("state", "") or "",
            merged_at=item.get("mergedAt"),
            head_ref=item.get("headRefName"),
        )
        for item in payload
    ]
    logger.info("Collected %d pull requests via gh", len(prs))
    return prs


def _gh_api(repo_path: Path, endpoint: str, timeout: int = 30) -> tuple[object, str | None]:
    """Call ``gh api <endpoint>`` in the repo. Returns ``(payload, error_kind)``.

    ``error_kind`` is ``None`` on success, else one of ``"not-found"`` (404),
    ``"forbidden"`` (403 — admin-only endpoint, or plan-gated feature), or ``"error"``.
    """
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            cwd=str(repo_path),
            timeout=timeout,
        )
        return json.loads(result.stdout), None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if "HTTP 404" in stderr:
            return None, "not-found"
        if "HTTP 403" in stderr:
            return None, "forbidden"
        logger.warning("gh api %s failed: %s", endpoint, stderr.strip()[:200])
        return None, "error"
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("gh api %s failed: %s", endpoint, exc)
        return None, "error"


def collect_branch_protection(repo_path: Path) -> dict[str, object]:
    """Read the default branch's merge-gate configuration (best-effort, honest).

    Reads only what the current credentials can see and says so: the branch
    ``protected`` flag is readable with plain read access and is authoritative for
    presence (it covers both classic protection and rulesets); rule parameters come
    from the rulesets endpoint (read access) or the classic-protection endpoint
    (admin only — a 403 downgrades to "detail not readable", never to a guess).

    States: ``present`` / ``absent`` / ``not-collected``. This is the CURRENT
    configuration at collection time — deliberately recorded as a repo-level control,
    never used as per-change evidence that historical changes were gated (protection
    history is not readable from the API).
    """
    repo_info, err = _gh_api(repo_path, "repos/{owner}/{repo}")
    if not isinstance(repo_info, dict):
        return {
            "state": "not-collected",
            "note": f"GitHub repository not resolvable this run ({err}); "
                    f"merge-gate state was not read.",
        }
    default_branch = str(repo_info.get("default_branch") or "main")
    safe_branch = redact_quoted(default_branch)[0]

    branch, err = _gh_api(repo_path, f"repos/{{owner}}/{{repo}}/branches/{default_branch}")
    if not isinstance(branch, dict) or not isinstance(branch.get("protected"), bool):
        return {
            "state": "not-collected",
            "default_branch": safe_branch,
            "note": f"Default-branch metadata not readable this run ({err}); "
                    f"merge-gate state was not read.",
        }
    protected: bool = branch["protected"]

    result: dict[str, object] = {
        "state": "present" if protected else "absent",
        "default_branch": safe_branch,
        "rule_types": None,
        "required_approving_reviews": None,
        "require_code_owner_review": None,
        "requires_review": None,
        "collection": "gh api: branch metadata + rulesets/classic protection as readable",
    }
    if not protected:
        result["note"] = (
            f"The default branch (`{safe_branch}`) is not marked protected; no branch "
            f"protection or ruleset gates merges on it."
        )
        return result

    # Parameters — rulesets first (readable with read access), then classic detail.
    pr_rule: dict | None = None
    rules, rules_err = _gh_api(repo_path, f"repos/{{owner}}/{{repo}}/rules/branches/{default_branch}")
    rules_readable = isinstance(rules, list)
    if rules_readable:
        result["rule_types"] = sorted({str(r.get("type")) for r in rules if isinstance(r, dict)})
        pr_rule = next(
            (r for r in rules if isinstance(r, dict) and r.get("type") == "pull_request"), None
        )
    if pr_rule is not None:
        params = pr_rule.get("parameters") or {}
        result["required_approving_reviews"] = params.get("required_approving_review_count")
        result["require_code_owner_review"] = params.get("require_code_owner_review")
    else:
        detail, detail_err = _gh_api(
            repo_path, f"repos/{{owner}}/{{repo}}/branches/{default_branch}/protection"
        )
        if isinstance(detail, dict):
            reviews = detail.get("required_pull_request_reviews") or {}
            result["required_approving_reviews"] = reviews.get("required_approving_review_count")
            result["require_code_owner_review"] = reviews.get("require_code_owner_reviews")
        elif rules_readable and detail_err == "not-found":
            # The configuration IS fully known: the active rulesets carry no
            # pull-request rule and no classic protection exists — the gate does not
            # require a review. This is a finding, not an unknown.
            result["requires_review"] = False
            result["note"] = (
                f"The default branch (`{safe_branch}`) is protected (current "
                f"configuration read at collection time), but no pull-request review "
                f"requirement is configured (active rules: "
                f"{', '.join(result['rule_types'] or []) or 'none'}; no classic branch "
                f"protection)."
            )
            return result
        else:
            result["note"] = (
                f"The default branch (`{safe_branch}`) is marked protected, but the "
                f"protection configuration is not readable with the current credentials "
                f"(rulesets: {rules_err or 'no pull-request rule'}; classic protection: "
                f"{detail_err}). Presence is asserted from the branch metadata only; no "
                f"review-requirement detail is asserted."
            )
            return result

    count = result["required_approving_reviews"]
    codeowner = result["require_code_owner_review"]
    # Any positive signal is decisive; a negative verdict needs BOTH fields known —
    # a sparse payload (count unknown) must yield None, never a false "not required".
    if codeowner is True or (isinstance(count, int) and count >= 1):
        result["requires_review"] = True
    elif isinstance(count, int) and isinstance(codeowner, bool):
        result["requires_review"] = False
    review_phrase = (
        (f"{count} approving review(s) required" if isinstance(count, int)
         else "approving-review count not readable")
        + ("; code-owner review required" if codeowner else "")
    )
    result["note"] = (
        f"The default branch (`{safe_branch}`) is protected (current configuration read "
        f"at collection time): {review_phrase}."
    )
    return result


def summarize_merge_gate(bp: dict[str, object]) -> str:
    """One-line honest summary of the collected merge-gate state, for pack prose."""
    state = bp.get("state")
    if state == "present":
        branch = bp.get("default_branch", "default")
        count = bp.get("required_approving_reviews")
        codeowner = bp.get("require_code_owner_review")
        if count is None and codeowner is None:
            if bp.get("requires_review") is False:
                return (f"present on `{branch}` (no pull-request review requirement "
                        f"configured)")
            return (f"present on `{branch}` (configuration detail not readable with "
                    f"current credentials)")
        bits = [f"{count if isinstance(count, int) else 0} approving review(s) required"]
        if codeowner:
            bits.append("code-owner review required")
        return f"present on `{branch}` ({'; '.join(bits)})"
    if state == "absent":
        return f"not enabled on the default branch (`{bp.get('default_branch', 'default')}`)"
    return f"not determinable this run ({bp.get('note', 'not collected')})"


def _login(obj: object) -> str | None:
    """Extract a GitHub login from a gh actor object, or None."""
    return obj.get("login") if isinstance(obj, dict) else None


def _safe_login(login: str | None) -> str | None:
    """Neutralize any barred term in a GitHub login handle (defensive; handles are public)."""
    return neutralize_barred(login)[0] if login else None


def _excerpt_note(redacted: bool) -> str:
    """Build an honest note about any redactions applied to a quoted PR excerpt."""
    note = "Quoted from the PR body."
    if redacted:
        note += (
            " Redactions applied (integrity-claim terms, absolute paths, and/or emails), "
            "per the pack's no-overclaim and external-share rules."
        )
    return note


def collect_pr_details(repo_path: Path, numbers: list[int]) -> dict[int, dict]:
    """Fetch bounded, standalone PR evidence for each PR number via ``gh pr view``.

    Returns a mapping ``pr_number -> detail dict``. If gh is unavailable, returns an empty
    mapping and the caller records an honest "not collected: gh unavailable" note. Logins
    (public GitHub handles) are kept; no PII is introduced here.
    """
    if not gh_available():
        return {}
    details: dict[int, dict] = {}
    for number in sorted(set(numbers)):
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(number), "--json", _PR_VIEW_FIELDS],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                cwd=str(repo_path),
                timeout=30,
            )
            item = json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            logger.warning("gh pr view %s failed; skipping enrichment: %s", number, exc)
            continue
        reviews = [
            {
                "author": _safe_login(_login(r.get("author"))),
                "state": r.get("state"),
                "submitted_at": r.get("submittedAt"),
                "body_excerpt": redact_quoted(clean_excerpt(r.get("body") or "", max_chars=300))[0] or None,
            }
            for r in (item.get("reviews") or [])
        ]
        merge_commit = item.get("mergeCommit") or {}
        # Quoted repo text → the SAME unified redaction as every quoted field (barred
        # terms + paths + emails). A bespoke subset here is how the email leak survived.
        body_excerpt, redacted = redact_quoted(clean_excerpt(item.get("body") or "", max_chars=900))
        details[number] = {
            "number": number,
            "title": redact_quoted(item.get("title") or "")[0],
            "state": item.get("state"),
            "base_branch": redact_quoted(item.get("baseRefName") or "")[0] or None,
            "head_branch": redact_quoted(item.get("headRefName") or "")[0] or None,
            "author_login": _safe_login(_login(item.get("author"))),
            "merged_at": item.get("mergedAt"),
            "merged_by": _safe_login(_login(item.get("mergedBy"))),
            "merge_commit": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
            "reviews": reviews,
            "reviews_note": (
                "GitHub PR review objects (often empty for this repo): the recorded "
                "adversarial review is carried in the PR/commit body, not a GitHub review."
            ),
            "body_excerpt": body_excerpt,
            "body_excerpt_note": _excerpt_note(redacted),
            "checks": _checks_block(repo_path, item.get("headRefOid")),
            "collection": "gh pr view",
        }
    logger.info("Enriched %d pull-request records via gh pr view", len(details))
    return details
