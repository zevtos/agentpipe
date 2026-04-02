---
description: "Full project kickoff from an idea. Orchestrates pm → architect → dba → devops to produce specs, architecture, schema, and project scaffold."
argument-hint: <project idea or description>
---

You are orchestrating a full project kickoff. The user has an idea and wants a production-ready project foundation. Follow this pipeline EXACTLY, step by step. Do NOT skip steps. Do NOT implement code until all planning agents have completed.

## Context
@CLAUDE.md

## The Idea
$ARGUMENTS

## Pipeline

### Step 1: Requirements (PM Agent)
Run the `pm` agent with this prompt:
"The user wants to build: $ARGUMENTS
Create a comprehensive product specification including:
- Problem statement and target users
- Core user stories with acceptance criteria (Given/When/Then)
- Feature list prioritized as Must/Should/Could/Won't (MoSCoW)
- Non-functional requirements (performance targets, security requirements, scalability)
- Edge cases and error scenarios
- Open questions that need answers
- Success metrics"

Present the PM output to the user. Ask: "Does this spec capture what you want? Any changes or missing requirements?"
Wait for user confirmation before proceeding.

### Step 2: Architecture (Architect Agent)
Run the `architect` agent, passing the confirmed spec:
"Based on this product specification:
[paste confirmed PM output]

Design the system architecture:
- Technology stack selection with ADRs for each major choice
- System decomposition (bounded contexts, service boundaries)
- API contract design (endpoints, request/response shapes, error handling)
- Data model (entities, relationships, constraints)
- Component diagram (Mermaid)
- Integration patterns (sync/async, queues, external services)
- Infrastructure requirements
- Evolution path for future scaling"

Present the architecture to the user. Ask: "Does this architecture work for you? Any technology preferences or constraints I should adjust?"
Wait for user confirmation before proceeding.

### Step 3: Database Design (DBA Agent)
Run the `dba` agent, passing the architecture:
"Based on this system architecture:
[paste confirmed architect output — specifically the data model section]

Design the complete database schema:
- Table definitions with exact types, constraints, and indexes
- Migration files for initial schema setup
- Seed data if applicable
- Index strategy for expected query patterns
- Backup strategy recommendation
- ER diagram (Mermaid)"

### Step 4: Infrastructure Setup (DevOps Agent)
Run the `devops` agent:
"Based on this architecture:
[paste technology stack from architect output]

Set up the project infrastructure:
- Dockerfile (multi-stage, production-hardened)
- docker-compose.yml for local development (app + database + any backing services)
- CI/CD pipeline (GitHub Actions) with: lint, test, build, security scan, deploy stages
- .env.example with all required environment variables documented
- Health check endpoints specification
- Structured logging configuration
- Basic monitoring setup"

### Step 5: Project Scaffold
Based on ALL the agent outputs above, create the initial project structure:
- Initialize the project directory with the chosen tech stack
- Create the folder structure following the architecture design
- Set up configuration files (linter, formatter, test config, etc.)
- Create initial README.md with project overview, setup instructions, and architecture summary
- Create the database migration files from the DBA output
- Create the Dockerfile and docker-compose.yml from the DevOps output
- Create the CI/CD pipeline configuration
- Create a CLAUDE.md for the project with conventions, commands, and architecture overview
- Create initial ADR files from the architect's decisions
- Initialize git with a proper .gitignore

### Step 6: Summary
Present a final summary:
- What was created and where
- Development setup instructions (how to run locally)
- Immediate next steps (what to implement first, based on MoSCoW priorities)
- Known gaps or decisions that need more research

Do NOT commit to git — let the user review everything first.
