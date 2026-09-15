#!/usr/bin/env bash
# Scan for secrets before committing.
#
# Two scans, because they answer different questions:
#
#   staged   what you are ABOUT to commit. This is the one that prevents a leak.
#   history  what is already committed. This detects a leak that happened.
#
# A history scan alone is detection, not prevention, and a scan run after a
# public push cannot undo the exposure. Install the hook below if you want this
# to run automatically:
#
#   ln -s ../../scripts/verify_no_secrets.sh .git/hooks/pre-commit
#
# The symlink target is relative to .git/hooks/, which is why it has two levels
# of "..".
set -euo pipefail

# Find the repository root. `dirname "$0"/..` is wrong when this runs as a git
# hook: $0 is then .git/hooks/pre-commit, so ".." lands in .git rather than the
# project. rev-parse works however it was invoked.
cd "$(git rev-parse --show-toplevel)"

if ! command -v gitleaks >/dev/null 2>&1; then
    echo "gitleaks not installed: brew install gitleaks" >&2
    exit 127
fi

echo "scanning staged changes..."
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    gitleaks protect --staged --config .gitleaks.toml --no-banner --redact
else
    # No commits yet, so there is nothing to diff against.
    #
    # Scanning the working tree here would be wrong: it reads files git will
    # never commit, and `.env` holds real credentials by design. Flagging those
    # is a true finding about your disk and a false one about your commit, and a
    # scanner that cries wolf on the first commit is a scanner people disable.
    #
    # So export the index to a temporary directory and scan exactly that. The
    # index is precisely what the commit will contain.
    echo "no HEAD yet: scanning the staged index"
    staged_copy="$(mktemp -d)"
    trap 'rm -rf "$staged_copy"' EXIT
    git checkout-index --all --force --prefix="$staged_copy/"
    cp .gitleaks.toml "$staged_copy/" 2>/dev/null || true
    gitleaks detect --no-git --config .gitleaks.toml --no-banner --redact \
        --source "$staged_copy"
fi

echo "scanning history..."
gitleaks detect --config .gitleaks.toml --no-banner --redact --source .

if [ -d n8n/workflows ]; then
    echo "checking workflow exports..."
    python3 scripts/check_workflow_exports.py
fi

echo "no secrets found"