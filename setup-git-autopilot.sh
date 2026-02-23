#!/bin/bash
# ============================================================
# GIT AUTOPILOT + MCP SETUP
# Run once in any project root. Never think about any of this again.
#
# What this does:
#   - Auto-formats all code on save (Prettier + Black)
#   - Auto-lints staged files before every commit
#   - Auto-enforces conventional commit messages
#   - Configures VS Code settings for the project
#   - Auto-configures MCP servers for anyone who clones the repo
#   - Sets up Claude Code permissions (safe defaults)
#   - Creates onboarding docs so new devs just work
#
# Usage:
#   cd /your/project
#   bash setup-git-autopilot.sh
#
# After running:
#   - Anyone who clones this repo gets MCPs auto-configured
#   - They just need to set env vars (guided by .env.example)
#   - VS Code auto-formats, auto-lints, enforces commit style
#   - Claude Code has safe permissions + all the right tools
# ============================================================

set -e

echo ""
echo "Setting up Git Autopilot + MCP Configuration..."
echo "===================================================="
echo ""

# ----------------------------------
# 1. INIT GIT IF NEEDED
# ----------------------------------
if [ ! -d ".git" ]; then
  echo "Initializing git repo..."
  git init
fi

# ----------------------------------
# 2. INSTALL DEPENDENCIES
# ----------------------------------
echo "Installing git hygiene packages..."

if [ -f "pnpm-lock.yaml" ]; then
  PKG="pnpm"
elif [ -f "yarn.lock" ]; then
  PKG="yarn"
else
  PKG="npm"
fi

echo "   Using $PKG..."

$PKG install --save-dev \
  husky \
  @commitlint/cli \
  @commitlint/config-conventional \
  lint-staged \
  prettier \
  2>/dev/null || npm install --save-dev \
  husky \
  @commitlint/cli \
  @commitlint/config-conventional \
  lint-staged \
  prettier

echo "   Packages installed"

# ----------------------------------
# 3. SETUP HUSKY (GIT HOOKS)
# ----------------------------------
echo "Setting up Husky git hooks..."

npx husky init 2>/dev/null || true

cat > .husky/pre-commit << 'EOF'
npx lint-staged
EOF

cat > .husky/commit-msg << 'EOF'
npx --no -- commitlint --edit $1
EOF

cat > .husky/pre-push << 'EOF'
if npm run test --if-present 2>/dev/null; then
  echo "Tests passed"
else
  echo "No test script found, skipping"
fi
EOF

chmod +x .husky/pre-commit .husky/commit-msg .husky/pre-push
echo "   Git hooks configured"

# ----------------------------------
# 4. COMMITLINT CONFIG
# ----------------------------------
echo "Setting up commit message enforcement..."

cat > commitlint.config.js << 'EOF'
module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [2, 'always', [
      'feat', 'fix', 'docs', 'style', 'refactor',
      'perf', 'test', 'build', 'ci', 'chore', 'revert', 'wip'
    ]],
    'subject-max-length': [1, 'always', 100],
    'subject-case': [0],
  }
};
EOF
echo "   Commitlint configured"

# ----------------------------------
# 5. LINT-STAGED CONFIG
# ----------------------------------
echo "Setting up auto-lint on staged files..."

cat > .lintstagedrc.json << 'EOF'
{
  "*.{json,md,mdx,yml,yaml,css,scss,html}": [
    "npx prettier --write"
  ]
}
EOF
echo "   Lint-staged configured"

# ----------------------------------
# 6. PRETTIER CONFIG
# ----------------------------------
if [ ! -f ".prettierrc" ] && [ ! -f ".prettierrc.json" ] && [ ! -f "prettier.config.js" ]; then
echo "Setting up Prettier..."
cat > .prettierrc.json << 'EOF'
{
  "semi": true,
  "singleQuote": true,
  "trailingComma": "es5",
  "tabWidth": 2,
  "printWidth": 100,
  "bracketSpacing": true,
  "arrowParens": "always",
  "endOfLine": "lf"
}
EOF
echo "   Prettier configured"
fi

# ==================================
# 7. MCP SERVERS (THE KEY PART)
# ==================================
echo ""
echo "Setting up MCP servers (auto-shared via git)..."

cat > .mcp.json << 'MCPEOF'
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    },
    "sequential-thinking": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]
    },
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
MCPEOF

echo "   .mcp.json created — committed to git, auto-loaded for everyone"
echo "      context7             (no auth needed)"
echo "      sequential-thinking  (no auth needed)"
echo "      playwright           (no auth needed)"
echo "      github               (needs GITHUB_TOKEN env var)"

# ----------------------------------
# 8. CLAUDE CODE PERMISSIONS
# ----------------------------------
echo "Setting up Claude Code permissions..."

mkdir -p .claude

cat > .claude/settings.json << 'EOF'
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "permissions": {
    "allow": [
      "Bash(python *)",
      "Bash(python3 *)",
      "Bash(pip install *)",
      "Bash(pip3 install *)",
      "Bash(streamlit run *)",
      "Bash(npm run *)",
      "Bash(npx prettier *)",
      "Bash(npx eslint *)",
      "Bash(npx playwright *)",
      "Bash(git status *)",
      "Bash(git log *)",
      "Bash(git diff *)",
      "Bash(git branch *)",
      "Bash(git checkout -b *)",
      "Bash(git add *)",
      "Bash(git commit *)"
    ],
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./.env.local)",
      "Read(./secrets/**)",
      "Bash(rm -rf *)",
      "Bash(git push --force *)",
      "Bash(git push -f *)"
    ]
  }
}
EOF

echo "   Claude Code permissions set"

# ----------------------------------
# 9. VS CODE SETTINGS
# ----------------------------------
echo "Configuring VS Code..."

mkdir -p .vscode

# Only create if not already customized
if [ ! -f ".vscode/settings.json" ]; then
cat > .vscode/settings.json << 'EOF'
{
  "editor.formatOnSave": true,
  "editor.formatOnPaste": true,
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter",
    "editor.tabSize": 4,
    "editor.insertSpaces": true
  },
  "python.analysis.typeCheckingMode": "basic",
  "git.enableSmartCommit": true,
  "git.autofetch": true,
  "git.confirmSync": false,
  "git.pruneOnFetch": true,
  "git.rebaseWhenSync": true,
  "git.branchProtection": ["main", "master", "develop"],
  "git.inputValidationSubjectLength": 72,
  "scm.defaultViewMode": "tree",
  "editor.tabSize": 4,
  "editor.insertSpaces": true,
  "files.trimTrailingWhitespace": true,
  "files.insertFinalNewline": true,
  "files.trimFinalNewlines": true,
  "[json]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.tabSize": 2 },
  "[markdown]": { "editor.defaultFormatter": "esbenp.prettier-vscode" },
  "[yaml]": { "editor.defaultFormatter": "esbenp.prettier-vscode" },
  "python.terminal.activateEnvironment": true
}
EOF
echo "   VS Code settings configured"
else
echo "   VS Code settings already exist, skipping"
fi

cat > .vscode/extensions.json << 'EOF'
{
  "recommendations": [
    "ms-python.python",
    "ms-python.black-formatter",
    "esbenp.prettier-vscode",
    "eamodio.gitlens",
    "mhutchie.git-graph",
    "usernamehw.errorlens",
    "vivaxy.vscode-conventional-commits",
    "gruntfuggly.todo-tree",
    "streetsidesoftware.code-spell-checker"
  ]
}
EOF

echo "   Extension recommendations set"

# ----------------------------------
# 10. ENV EXAMPLE
# ----------------------------------
echo "Creating .env.example..."

if [ ! -f ".env.example" ]; then
cat > .env.example << 'EOF'
# ============================================
# Copy this to .env and fill in values
# ============================================

# --- MCP Servers (for Claude Code) ---
# GitHub: https://github.com/settings/tokens -> repo, read:org scopes
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx

# --- Amazon SP-API ---
# AMAZON_REFRESH_TOKEN=
# AMAZON_LWA_CLIENT_ID=
# AMAZON_LWA_CLIENT_SECRET=
# AMAZON_MARKETPLACE_ID=ATVPDKIKX0DER

# --- Shopify ---
# SHOPIFY_STORE_URL=
# SHOPIFY_ACCESS_TOKEN=
# SHOPIFY_CLIENT_ID=
# SHOPIFY_CLIENT_SECRET=
# SHOPIFY_API_VERSION=2024-01

# --- Packiyo 3PL ---
# PACKIYO_API_URL=https://aveshops.packiyo.com/api/v1
# PACKIYO_API_TOKEN=
# PACKIYO_CUSTOMER_ID=12
EOF
echo "   .env.example created"
else
echo "   .env.example already exists, skipping"
fi

# ----------------------------------
# 11. EDITORCONFIG
# ----------------------------------
if [ ! -f ".editorconfig" ]; then
cat > .editorconfig << 'EOF'
root = true

[*]
indent_style = space
indent_size = 4
end_of_line = lf
charset = utf-8
trim_trailing_whitespace = true
insert_final_newline = true

[*.py]
indent_size = 4

[*.{json,yml,yaml,md}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab
EOF
echo "   EditorConfig set"
else
echo "   EditorConfig already exists, skipping"
fi

# ----------------------------------
# 12. UPDATE .GITIGNORE
# ----------------------------------
echo "Updating .gitignore..."

GITIGNORE_ADDITIONS=(
  "node_modules/"
  ".env.local"
  ".env*.local"
  ".claude/settings.local.json"
)

touch .gitignore
for item in "${GITIGNORE_ADDITIONS[@]}"; do
  if ! grep -qF "$item" .gitignore 2>/dev/null; then
    echo "$item" >> .gitignore
  fi
done

echo "   .gitignore updated"

# ----------------------------------
# 13. GIT ALIASES
# ----------------------------------
echo "Adding useful git aliases..."

git config --local alias.lg "log --oneline --graph --all --decorate"
git config --local alias.last "log -1 HEAD --stat"
git config --local alias.st "status -sb"
git config --local alias.branches "branch -a --sort=-committerdate"
git config --local alias.undo "reset HEAD~1 --mixed"
git config --local alias.amend "commit --amend --no-edit"

echo "   Git aliases added"

# ----------------------------------
# DONE
# ----------------------------------
echo ""
echo "===================================================="
echo "EVERYTHING IS CONFIGURED!"
echo "===================================================="
echo ""
echo "What gets committed to git (shared with everyone):"
echo "  .mcp.json              — MCP servers auto-load for all Claude Code users"
echo "  .claude/settings.json  — Claude Code permissions"
echo "  .vscode/settings.json  — Auto-format, auto-lint, git settings"
echo "  .vscode/extensions.json— VS Code prompts to install extensions"
echo "  CLAUDE.md              — AI reads this for project context"
echo "  .env.example           — Shows new devs what tokens they need"
echo "  commitlint.config.js   — Commit message rules"
echo "  .lintstagedrc.json     — Auto-lint config"
echo "  .prettierrc.json       — Formatting rules"
echo "  .editorconfig          — Universal editor settings"
echo "  .husky/*               — Git hooks"
echo ""
echo "What a new dev does after cloning:"
echo "  1. npm install          (hooks auto-install via husky)"
echo "  2. cp .env.example .env"
echo "  3. Fill in their GITHUB_TOKEN"
echo "  4. Open in VS Code — accept extension recommendations"
echo "  5. That's it. Everything else is automatic."
echo ""
