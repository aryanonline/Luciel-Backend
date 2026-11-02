#!/usr/bin/env bash
# Post a markdown body on the month's "Dependency & CVE review YYYY-MM" issue
# (round 7 WP-9).
#
#   scripts/post_review_issue.sh <body-file>
#
# The monthly steward routine (a cloud routine, outside this repo) reads ONE issue
# per month, by this exact title. Every workflow that reports on dependencies or
# image CVEs posts through this script so the title can never drift between them:
#   * ci.yml               - the daily scheduled run, when it fails;
#   * image-rebuild.yml    - the weekly rebuild's gate report + tracker evidence;
#   * dependency-review.yml - the monthly review itself.
# Comments on the month's open issue, or opens it (label `dependencies`) when there
# is none. Needs GH_TOKEN with issues:write and REPO (owner/name); REVIEW_MONTH
# (YYYY-MM) overrides the current UTC month.
set -euo pipefail

BODY="${1:?markdown body file}"
REPO="${REPO:-${GITHUB_REPOSITORY:?REPO or GITHUB_REPOSITORY}}"
month="${REVIEW_MONTH:-$(date -u +%Y-%m)}"
title="Dependency & CVE review $month"

# Exact-title match over the REST issues list, which is read-after-write consistent.
# `gh issue list --search`, and even `--label`, go through GitHub's search index,
# which is eventually consistent: an issue another poster opened moments ago could
# be missed and the month would get two issues (ci.yml's failure poster, the weekly
# image-rebuild and this monthly review can all fire on the same 1st). The list
# carries pull requests too, so they are filtered out; no label filter, so an issue
# a person or the steward routine opened without one is still found.
# jq is a separate process because gh refuses --slurp together with --jq, and --slurp is
# what makes the pages one array. pipefail (above) stops the script if the list fails, so
# a refused read can never open a second issue.
existing=$(gh api --paginate --slurp "repos/$REPO/issues?state=open&per_page=100" |
  jq -r "[.[][] | select(.pull_request == null and .title == \"$title\")][0].number")
if [ -n "$existing" ] && [ "$existing" != "null" ]; then
  gh issue comment "$existing" --repo "$REPO" --body-file "$BODY"
  echo "commented on #$existing ($title)"
else
  gh issue create --repo "$REPO" --title "$title" --label dependencies --body-file "$BODY"
fi
