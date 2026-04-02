---
description: "Project orientation: understand a codebase's architecture, conventions, tech stack, and current state. Use when entering a new project or returning after a break."
---

You are orienting yourself (and the user) to a project. Explore the codebase systematically and produce a clear mental map.

## Context
@CLAUDE.md

## Pipeline

### Step 1: Project Identity
Gather basic facts:
1. Read CLAUDE.md, README.md, and any project documentation
2. Check package.json / pyproject.toml / Cargo.toml / go.mod for tech stack and dependencies
3. Check git log for recent activity (last 20 commits)
4. Check for CI/CD configuration (.github/workflows/, .gitlab-ci.yml, Jenkinsfile)
5. Check for containerization (Dockerfile, docker-compose.yml)

### Step 2: Architecture Map
Explore the codebase structure:
1. List top-level directory structure
2. Identify the main entry point(s)
3. Map the module/package structure
4. Identify external service integrations (APIs, databases, queues, caches)
5. Identify the data model (ORM models, schema files, migrations)
6. Identify the API surface (routes, controllers, handlers)
7. Check for test structure and coverage approach

### Step 3: Development Workflow
Identify:
1. How to install dependencies
2. How to run the project locally
3. How to run tests
4. How to build for production
5. How to deploy
6. Environment variables needed (.env.example)
7. Git workflow (branching strategy, PR process)

### Step 4: Current State
1. Any open TODOs/FIXMEs in the code
2. Dependency health (outdated packages, known vulnerabilities)
3. Test suite status (run tests if possible)
4. Recent changes (what's been worked on)

### Step 5: Report

```
## Project Overview: [name]

### Tech Stack
- Language: [language + version]
- Framework: [framework + version]
- Database: [DB + version]
- Key dependencies: [list major deps]

### Architecture
[Brief description + Mermaid component diagram]
- Entry points: [main files]
- API layer: [where routes/handlers live]
- Business logic: [where domain logic lives]
- Data layer: [where DB access lives]
- External integrations: [services used]

### Directory Structure
[Annotated tree of key directories]

### Development Commands
| Action | Command |
|--------|---------|
| Install deps | `...` |
| Run dev server | `...` |
| Run tests | `...` |
| Build | `...` |
| Lint | `...` |

### Conventions
[Coding patterns, naming conventions, project-specific rules]

### Current State
- Last activity: [date + what was worked on]
- Test status: [passing/failing/unknown]
- Known issues: [TODOs, FIXMEs, outdated deps]

### Recommended First Steps
[What to do next based on the project state]
```

If a CLAUDE.md doesn't exist or is incomplete, offer to create/update it based on what was discovered.
