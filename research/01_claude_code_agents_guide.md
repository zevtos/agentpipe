# Claude Code custom agents: the definitive 2025–2026 guide

**Claude Code's agent ecosystem has matured into a layered architecture of CLAUDE.md files, custom subagents, skills, hooks, and orchestration patterns** that transforms a single AI coding assistant into a coordinated multi-agent development team. The system's power comes from three interlocking primitives: CLAUDE.md for persistent project context, `.claude/agents/` for specialized personas, and `.claude/skills/` for reusable workflows — all wired together with hooks, MCP servers, and the Claude Agent SDK. This guide distills the practical, implementation-level patterns that power users and Anthropic's own documentation have converged on through 2025–2026.

---

## The `.claude/` directory is your agent control plane

Every Claude Code project revolves around a well-structured `.claude/` directory. The canonical layout, synthesized from official docs and community best practices, looks like this:

```
your-project/
├── CLAUDE.md                        # Project instructions (committed to git)
├── CLAUDE.local.md                  # Personal overrides (gitignored)
└── .claude/
    ├── settings.json                # Permissions, hooks config (committed)
    ├── settings.local.json          # Local overrides (gitignored)
    ├── agents/                      # Custom subagent personas
    │   ├── code-reviewer.md
    │   ├── security-auditor.md
    │   └── architect.md
    ├── skills/                      # Auto-invocable workflows
    │   ├── explain-code/
    │   │   ├── SKILL.md
    │   │   └── examples/
    │   └── deploy/
    │       ├── SKILL.md
    │       └── templates/
    ├── commands/                    # Legacy slash commands (still works)
    │   ├── review.md               # → /project:review
    │   └── fix-issue.md            # → /project:fix-issue
    ├── rules/                      # Modular instruction files (auto-loaded)
    │   ├── code-style.md
    │   └── api-conventions.md
    └── output-styles/              # Custom response formats

~/.claude/                          # Global (personal, all projects)
├── CLAUDE.md                       # Global instructions
├── agents/                         # Personal agents (lower priority)
├── skills/                         # Personal skills
├── commands/                       # Personal commands → /user:name
└── projects/<project>/memory/      # Auto-memory (Claude writes these)
    ├── MEMORY.md                   # Index, loaded every session
    └── <topic>.md                  # Detailed notes per topic
```

**CLAUDE.md files are hierarchical.** Claude loads `~/.claude/CLAUDE.md` (global), then the project root `CLAUDE.md`, then subdirectory CLAUDE.md files lazily as it navigates into folders. All survive context compaction — after `/compact`, Claude re-reads them from disk. Project-level agents override global agents with the same name. In monorepos, use `claudeMdExcludes` in `.claude/settings.local.json` to skip irrelevant ancestor files.

---

## Writing an effective CLAUDE.md

The community has converged on a clear consensus: **CLAUDE.md is the highest-leverage file in your entire project**, but only if kept concise and carefully crafted. Claude Code wraps CLAUDE.md content in a `<system-reminder>` tag that explicitly tells Claude the context "may or may not be relevant," meaning instructions perceived as irrelevant can be ignored.

**The golden rules** from power users and Anthropic engineers:

Keep it **under 200 lines** (60 lines proved even more reliable in one study). Boris Cherny, Claude Code's creator, keeps his own at roughly 2,500 tokens. Structure content around three pillars: WHAT (tech stack), WHY (project purpose), and HOW (build commands, conventions). Never use CLAUDE.md as a linter — use deterministic tools instead. Don't auto-generate it with `/init` for production. Every line should be hand-crafted.

Here's a battle-tested minimal CLAUDE.md that demonstrates the pattern:

```markdown
# Project: Acme API

## Commands
npm run dev          # Start dev server
npm run test         # Run tests (Jest)
npm run lint         # ESLint + Prettier check
npm run build        # Production build

## Architecture
- Express REST API, Node 20
- PostgreSQL via Prisma ORM
- All handlers in src/handlers/
- Shared types in src/types/

## Conventions
- Use zod for request validation in every handler
- Return shape: always { data, error }
- Never expose stack traces to the client
- Use the logger module, not console.log

## Watch out for
- Tests use a real local DB, not mocks. Run `npm run db:test:reset` first
- Strict TypeScript: no unused imports, ever
```

For larger projects, power users employ **progressive disclosure** — keeping detailed docs in separate files and pointing Claude to them with context on *when* to read:

```markdown
## Reference Docs
- For complex CSS work: `agent_docs/ADDING_CSS.md`
- For database schema changes: `agent_docs/database_schema.md`
- If you encounter a FooBarError: `agent_docs/troubleshooting.md`
Read the relevant file before starting work on that area.
```

The `.claude/rules/` directory extends this pattern further. Rules files can be **path-scoped** with frontmatter to apply only when Claude works in specific directories:

```yaml
---
paths:
  - "src/api/**/*.ts"
  - "src/handlers/**/*.ts"
---
# API Design Rules
- All handlers return { data, error } shape
- Use middleware for auth validation
```

**Anti-patterns to avoid**: Don't `@`-import entire files (bloats every session). Don't just say "Never X" without providing an alternative. Don't include code style rules that a linter can enforce. Don't include task-specific instructions — only universally applicable rules belong in CLAUDE.md. At enterprise scale, Shrivu Shankar recommends treating CLAUDE.md like "ad space" — each internal tool gets a maximum token budget, and if you can't explain it concisely, the tool isn't ready for CLAUDE.md.

---

## Custom subagents turn Claude into a team

Subagents are the primary building block for multi-agent work. Each runs in its **own context window** with a custom system prompt, specific tool access, and independent permissions. The parent receives only the subagent's final message — all intermediate work stays isolated.

### Defining subagents

Create a Markdown file with YAML frontmatter in `.claude/agents/`:

```markdown
---
name: code-reviewer
description: Expert code review specialist. MUST BE USED immediately after
  writing or modifying code. Use PROACTIVELY before any commit.
tools: Read, Grep, Glob, Bash
model: opus
memory: project
---

You are a senior code reviewer ensuring high standards of quality and security.

When invoked:
1. Run git diff to see recent changes
2. Focus on modified files only
3. Flag bugs, not just style issues
4. Suggest specific fixes, not vague improvements
5. Check for edge cases and error handling gaps
6. Note performance concerns only when they matter at scale
```

The **description field is a trigger, not a summary** — write it for the model to understand *when* to fire. Embedding action words like "MUST BE USED" or "PROACTIVELY" significantly increases auto-delegation frequency. The full frontmatter schema supports these fields:

| Field | Purpose |
|---|---|
| `name` | Agent identifier (lowercase, hyphens) |
| `description` | When Claude should invoke this agent |
| `tools` | Comma-separated allowlist; omit to inherit all |
| `model` | `haiku`, `sonnet`, `opus`, or `inherit` |
| `memory` | `user`, `project`, or `local` — enables persistent agent memory |
| `skills` | Skills to preload into agent context |
| `mcpServers` | MCP servers available to this agent |
| `isolation` | `worktree` for git-isolated execution |
| `hooks` | Lifecycle hooks specific to this agent |
| `maxTurns` | Maximum agentic turns before stopping |
| `background` | Run concurrently with main conversation |

**Key constraint**: subagents cannot spawn other subagents (no nesting). Chain them from the main conversation or use skills for nested delegation.

### Role-specific agent recipes

The community has standardized tool scoping by role:

- **Read-only agents** (reviewers, auditors): `tools: Read, Grep, Glob`
- **Research agents**: `tools: Read, Grep, Glob, WebFetch, WebSearch`
- **Code writers**: `tools: Read, Write, Edit, Bash, Glob, Grep`
- **Documentation agents**: `tools: Read, Write, Edit, Glob, Grep, WebFetch`

Here's a security auditor using the Opus model for maximum reasoning:

```markdown
---
name: security-auditor
description: Comprehensive security audit. MUST BE USED when reviewing code
  for vulnerabilities, before deployments, or when security is mentioned.
tools: Read, Grep, Glob, Bash
model: opus
---
Analyze the codebase for security vulnerabilities:
1. SQL injection and XSS risks
2. Exposed credentials or secrets in code and config
3. Authentication and authorization gaps
4. Dependency vulnerabilities (run npm audit or equivalent)
Report findings with severity ratings and specific remediation steps.
```

And a DevOps agent with write access:

```markdown
---
name: deployment-engineer
description: Handles Docker, Kubernetes, CI/CD config, and infrastructure code.
tools: Read, Write, Edit, Bash, Glob, Grep
model: sonnet
---
You understand deployment processes, Docker, Kubernetes, and CI/CD frameworks.
Optimize configurations and cross-reference with logs for debugging.
Focus on security best practices in infrastructure-as-code.
```

### Invoking and managing subagents

Claude automatically delegates when tasks match a subagent's description. You can also invoke explicitly: "Use the security-auditor subagent to review src/auth/." The `/agents` command provides interactive creation and management, and `claude agents` lists all configured agents from the command line. Starting Claude with `claude --agent <name>` uses a specific subagent as the primary agent.

**Built-in subagents** ship with Claude Code: **Explore** (Haiku, read-only file search), **Plan** (codebase research during plan mode), **general-purpose** (complex research and code modifications), and **Bash** (terminal commands in separate context).

---

## Skills and slash commands define reusable workflows

Skills are the modern evolution of slash commands, following the open **Agent Skills standard**. Both systems create `/command-name` invocations, but skills add directory structures for bundled scripts, templates, and auto-invocation.

### Slash commands (legacy, still works)

Place a Markdown file in `.claude/commands/`:

```markdown
---
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*)
argument-hint: [branch-name]
description: Review the current branch changes
model: claude-3-5-haiku-20241022
---

## Auto context
- Status: !`git status`
- Changed files: !`git diff --name-only`
- Diff summary: !`git diff --stat`

## Repo rules
@CLAUDE.md

## Task
Review the above changes for code quality, security vulnerabilities,
missing test coverage, and performance concerns.

Topic: $ARGUMENTS
```

Key variable interpolation: **`$ARGUMENTS`** captures all text after the command name, **`$1`, `$2`** are positional arguments, **`!`command``** executes shell commands inline and embeds output, and **`@filepath`** embeds file contents. Subdirectories create namespaced commands — `.claude/commands/dev/review.md` becomes `/dev:review`.

### Skills (recommended for new workflows)

Skills live in `.claude/skills/<name>/SKILL.md` with optional supporting files:

```markdown
---
name: deep-research
description: Research a topic thoroughly using parallel exploration
context: fork
agent: Explore
allowed-tools: Read, Grep, Glob
---

Research $ARGUMENTS thoroughly:
1. Find relevant files using Glob and Grep
2. Read and analyze the code
3. Summarize findings with specific file references
```

The `context: fork` directive runs the skill in an isolated subagent context, and the `agent` field specifies which subagent to use (built-in or custom). Skills can be auto-invoked by Claude when the description matches the current task — no explicit `/` needed. If a skill and command share the same name, **the skill takes precedence**.

### The Command → Agent → Skill architecture

The most powerful pattern chains all three primitives together. A command acts as the user-facing entry point, delegating to a specialized agent, which draws on preloaded skills:

```
/weather-orchestrator (command)
  → weather-agent (agent with skills: weather-fetcher, weather-svg-creator)
    → weather-fetcher (skill: API calls)
    → weather-svg-creator (skill: visualization)
```

This provides progressive disclosure — skills inject detailed knowledge only when the agent needs it, keeping base context lean.

---

## Hooks, Agent Teams, and advanced orchestration

### Hooks enforce deterministic guardrails

Hooks are shell commands that fire at **21 lifecycle events** including `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SubagentStop`, and `TaskCompleted`. They support four handler types: `command`, `http`, `prompt` (single-turn LLM evaluation), and `agent` (multi-turn subagent verification). Configure them in `settings.json`:

The most popular community hook auto-formats every file Claude edits:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit|MultiEdit",
      "hooks": [{
        "type": "command",
        "command": "npx prettier --write \"$CLAUDE_TOOL_INPUT_FILE_PATH\""
      }]
    }]
  }
}
```

Other battle-tested patterns include blocking destructive commands (`rm -rf`, `DROP TABLE`) via `PreToolUse` with exit code 2, injecting git branch context on `SessionStart`, running tests on `Stop` to prevent Claude from declaring "done" prematurely, and logging all bash commands with timestamps. Shrivu Shankar's enterprise approach: **don't block at write time** (confuses the agent mid-plan) — **block at commit time** using a pre-commit validation hook.

### Agent Teams enable true parallel execution

Agent Teams, available via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, run multiple independent Claude Code instances with inter-agent coordination. Unlike subagents (which run within a single session), Agent Teams operate across separate sessions with a **shared task list**, **peer-to-peer messaging**, and **file locking** to prevent merge conflicts.

The architecture has three layers: a **Team Lead** that decomposes work and synthesizes results, a **shared task list** with dependency tracking, and **Teammates** — independent Claude instances running in tmux split panes. Natural language invocation works: "create an agent team with 3 teammates to refactor these modules in parallel."

**Best practices for Agent Teams**: 3–5 teammates is the sweet spot (token costs scale linearly). Use them for genuinely parallelizable work across different modules or layers. Spawn a dedicated reviewer teammate (Opus, read-only tools) that auto-triggers on every `TaskCompleted` event. Agent Teams consume roughly **7× the tokens** of a standard session.

### The Claude Agent SDK for programmatic control

The SDK (renamed from Claude Code SDK to `@anthropic-ai/claude-agent-sdk` for TypeScript, `claude-agent-sdk` for Python) enables headless automation:

```python
from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition

async for message in query(
    prompt="Review the authentication module for security issues",
    options=ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["project"],  # Required to load CLAUDE.md
        allowed_tools=["Read", "Grep", "Glob", "Agent"],
        agents={
            "security-reviewer": AgentDefinition(
                description="Security code reviewer",
                prompt="You are a security expert. Find vulnerabilities.",
                tools=["Read", "Grep", "Glob"],
                model="opus",
            ),
        },
    ),
):
    print(message)
```

The `-p` flag runs Claude Code non-interactively for CI/CD: `claude -p "Review this PR" --max-turns 5 --max-budget-usd 1.50 --output-format json`. The official GitHub Action (`anthropics/claude-code-action@v1`) runs the full Claude Code runtime inside GitHub Actions runners for automated PR reviews and CI failure auto-fixes.

### Multi-model routing optimizes cost

Route different tasks to appropriate models: **Opus** for architecture decisions and tricky debugging, **Sonnet** for standard implementation, **Haiku** for docs, subagent exploration tasks, and high-volume boilerplate. Anthropic's own research found that an Opus lead with Sonnet subagents **outperformed single-agent Opus by 90.2%** on internal research evaluations, while using token allocation as the primary lever — token usage alone explained 80% of performance variance.

---

## Conclusion

The Claude Code agent ecosystem has evolved well beyond a simple AI coding assistant. The core insight is that **configuration is the multiplier** — the same model scored 78% with one harness and 42% with another in research benchmarks. Three design principles consistently emerge across all sources: keep CLAUDE.md lean and hand-crafted (under 200 lines), give each agent exactly one domain of expertise with minimal tool access, and use hooks for deterministic guarantees rather than relying on prompt compliance. The Command → Agent → Skill architecture provides elegant progressive disclosure, while Agent Teams and the Claude Agent SDK open the door to fully autonomous development pipelines. The emerging pattern of treating agent configuration as infrastructure-as-code — version-controlled, team-shared, and continuously refined based on failure analysis — represents the most impactful shift in how engineering teams work with AI coding tools in 2026.