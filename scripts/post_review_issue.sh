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

# Exact-title match: the search is a substring search, and a month's issue must not
# be confused with a differently titled one that merely contains this text.
existing=$(gh issue list --repo "$REPO" --state open --search "in:title \"$title\"" \
  --json number,title --jq "[.[] | select(.title == \"$title\")][0].number")
if [ -n "$existing" ] && [ "$existing" != "null" ]; then
  gh issue comment "$existing" --repo "$REPO" --body-file "$BODY"
  echo "commented on #$existing ($title)"
else
  gh issue create --repo "$REPO" --title "$title" --label dependencies --body-file "$BODY"
fi
