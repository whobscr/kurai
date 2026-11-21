
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "ERROR: Run this script inside the Kurai git repository."
    exit 1
}

cd "$ROOT"

BRANCH="refactor/rename-pulse-to-pulse"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: Working tree is not clean."
    echo "Commit or stash your changes before running this script."
    git status --short
    exit 1
fi

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    echo "ERROR: Branch $BRANCH already exists."
    exit 1
fi

echo "Creating branch: $BRANCH"
git switch -c "$BRANCH"

python3 <<'PY'
import os
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd()

# Historical Alembic migrations must remain immutable.
SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
}

SKIP_FILES = {
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

def should_skip(path: Path) -> bool:
    parts = set(path.parts)

    if parts & SKIP_PARTS:
        return True

    if path.name in SKIP_FILES:
        return True

    # Never rewrite migration history.
    if "migrations" in parts:
        return True

    return False


def rename_text(text: str) -> str:
    # Russian UI labels and common grammatical forms.
    russian = [
        (r"моя\s+волна", "Пульс"),
        (r"мою\s+волну", "Пульс"),
        (r"моей\s+волны", "Пульс"),
        (r"моей\s+волне", "Пульс"),
        (r"my\s+pulse", "Pulse"),
    ]

    for pattern, replacement in russian:
        text = re.sub(
            pattern,
            replacement,
            text,
            flags=re.IGNORECASE,
        )

    # All code identifiers, class names, comments and references.
    # Preserve the original capitalization.
    def replace_identifier(match):
        value = match.group(0)

        if value.isupper():
            return "PULSE"
        if value[0].isupper():
            return "Pulse"
        return "pulse"

    text = re.sub(r"pulse", replace_identifier, text, flags=re.IGNORECASE)

    return text


def is_historical_migration(path: Path) -> bool:
    return "migrations" in path.parts


# Obtain tracked files from Git.
result = subprocess.run(
    ["git", "ls-files", "-z"],
    check=True,
    capture_output=True,
)

files = [
    ROOT / name.decode()
    for name in result.stdout.split(b"\0")
    if name
]

changed_contents = 0
renamed_files = 0
skipped = 0

print("\n=== Rewriting file contents ===")

for path in files:
    if not path.is_file() or should_skip(path.relative_to(ROOT)):
        continue

    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (UnicodeDecodeError, OSError):
        skipped += 1
        continue

    updated = rename_text(text)

    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
        changed_contents += 1
        print(f"MODIFIED: {path.relative_to(ROOT)}")

print("\n=== Renaming files ===")

# Deepest paths first, so directories can be renamed safely.
paths = sorted(
    [p for p in ROOT.rglob("*") if p.exists()],
    key=lambda p: len(p.parts),
    reverse=True,
)

for path in paths:
    relative = path.relative_to(ROOT)

    if should_skip(relative):
        continue

    new_name = rename_text(path.name)

    if new_name == path.name:
        continue

    destination = path.with_name(new_name)

    if destination.exists():
        raise RuntimeError(
            f"Rename collision: {path} -> {destination}"
        )

    path.rename(destination)
    renamed_files += 1

    print(f"RENAMED: {relative} -> {destination.relative_to(ROOT)}")

print("\n=== Summary ===")
print(f"Modified files: {changed_contents}")
print(f"Renamed paths:  {renamed_files}")
print(f"Skipped files:  {skipped}")

PY

echo
echo "=== Remaining references (excluding historical migrations) ==="

if command -v rg >/dev/null 2>&1; then
    rg -n -i 'pulse|моя[[:space:]]+волна|мою[[:space:]]+волну' \
        --hidden \
        -g '!.git/**' \
        -g '!**/migrations/**' \
        -g '!**/vendor/**' \
        -g '!**/uv.lock' \
        . || true
else
    echo "Install ripgrep to perform the final scan: sudo apt install ripgrep"
fi

echo
echo "=== Git status ==="
git status --short

echo
echo "Done. Review the changes before committing."
echo "Branch: $BRANCH"

