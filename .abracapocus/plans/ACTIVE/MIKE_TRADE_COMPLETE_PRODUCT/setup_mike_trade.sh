#!/usr/bin/env bash
# Bootstrap the private mike_trade GitHub repository on the Ubuntu DEV machine.
# Safe to rerun: never overwrites existing files or replaces an existing git remote.
set -euo pipefail

ROOT="$HOME/dev/mike/stocks/python/mike_trade"
REPO="mike_trade"

for command in git gh; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: '$command' is required." >&2
    exit 1
  }
done

gh auth status >/dev/null || {
  echo "ERROR: GitHub CLI not logged in. Run: gh auth login" >&2
  exit 1
}

if [[ -d "$ROOT/.git" ]]; then
  echo "Existing git repository: $ROOT"
elif [[ -e "$ROOT" && -n "$(ls -A "$ROOT" 2>/dev/null)" ]]; then
  echo "ERROR: $ROOT exists and is not an empty directory or initialized git repo." >&2
  echo "Refusing to initialize inside an existing nonempty directory." >&2
  exit 1
else
  mkdir -p "$ROOT"
  git -C "$ROOT" init -b main
fi

mkdir -p "$ROOT/.abracapocus"

if [[ ! -f "$ROOT/README.md" ]]; then
  cat > "$ROOT/README.md" <<'EOF'
# mike_trade

Self-hosted, multi-stock, strategy-driven trading system with Alpaca,
shadow accounts, backtesting, a React dashboard and Makefile operations.

The complete Abracapocus plan lives in `.abracapocus/`:

- `PLAN_SKELETON.json` (copy in after bootstrap)
- `PRODUCT_SPEC.md` (copy from the plan package)
- `MATERIALIZATION_GUIDE.md` (copy from the plan package)

Do not enable live brokerage execution until broker capabilities, paper tests,
configuration and operator permissions have been verified.
EOF
fi

if [[ ! -f "$ROOT/.gitignore" ]]; then
  cat > "$ROOT/.gitignore" <<'EOF'
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.env
.env.*
!.env.example
state/
node_modules/
dist/
build/
*.egg-info/
.coverage
htmlcov/
EOF
fi

# Keep the Abracapocus directory in the initial commit before plans are copied.
touch "$ROOT/.abracapocus/.gitkeep"

if [[ -d "$ROOT/.git" ]]; then
  git -C "$ROOT" branch --show-current >/dev/null
fi

# Initial, bounded commit: avoid staging any unrelated existing user files.
git -C "$ROOT" add -- README.md .gitignore .abracapocus/.gitkeep
if ! git -C "$ROOT" diff --cached --quiet; then
  git -C "$ROOT" commit -m "chore: initialize mike_trade repository"
fi

if git -C "$ROOT" remote get-url origin >/dev/null 2>&1; then
  echo "Existing origin: $(git -C "$ROOT" remote get-url origin)"
  echo "GitHub repository creation skipped; origin already configured."
else
  echo "Creating private GitHub repository $REPO and pushing initial commit..."
  (
    cd "$ROOT"
    gh repo create "$REPO" --private --source=. --remote=origin --push
  )
fi

cat <<EOF

Local repository: $ROOT
Abracapocus dir: $ROOT/.abracapocus
GitHub origin:    $(git -C "$ROOT" remote get-url origin)

Next: copy PLAN_SKELETON.json and PRODUCT_SPEC.md from the
MIKE_TRADE_ABRACAPOCUS_PLAN package into $ROOT/.abracapocus/
Then follow MATERIALIZATION_GUIDE.md. Do not run plan-shape.
EOF
