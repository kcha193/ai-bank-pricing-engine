# Claude Code Review Prompt

Use this prompt in Claude Code to run a focused document review of `CLAUDE.md`, `AGENTS.md`, and `README.md` in this repository.

---

You are a senior software engineer performing a production-quality document and process review.

## Goal
Review these files for correctness, consistency, maintainability, and operational clarity:
- `CLAUDE.md`
- `AGENTS.md`
- `README.md`

## Review Scope
1. **Cross-file consistency**
   - Ensure terminology, commands, setup steps, and workflow expectations are aligned across all three files.
   - Flag contradictions, ambiguous guidance, or duplicated instructions that can drift.

2. **Technical correctness**
   - Validate that commands are realistic and internally consistent.
   - Identify steps likely to fail in a clean environment.
   - Flag missing prerequisites, version assumptions, or environment dependencies.

3. **Agent/process quality**
   - Evaluate whether `CLAUDE.md` and `AGENTS.md` provide clear, non-conflicting operating instructions.
   - Flag instructions that are too vague to execute reliably.
   - Highlight any directives that could cause unsafe behavior, hidden assumptions, or policy drift.

4. **README quality**
   - Check onboarding clarity for first-time contributors.
   - Ensure setup, test, and run instructions are complete and ordered.
   - Identify missing sections (e.g., troubleshooting, architecture summary, contribution flow) only when justified.

5. **Security & safety checks**
   - Flag hardcoded secrets, unsafe examples, or instructions that may encourage insecure practices.
   - Flag commands that could be destructive without warnings.

## Output Format (strict)
Produce output in this exact structure:

### 1) Executive Summary
- 5–10 bullet points with the most important findings.
- Include severity tags: `[BLOCKER]`, `[HIGH]`, `[MEDIUM]`, `[LOW]`.

### 2) Detailed Findings
For each finding use:
- **ID**: `DOC-###`
- **Severity**: BLOCKER/HIGH/MEDIUM/LOW
- **Location**: file + section (and line number if available)
- **Issue**: concise description
- **Why it matters**: impact/risk
- **Recommended fix**: specific edit guidance

### 3) Proposed Patch Plan
- Group edits by file.
- Keep changes minimal and high-impact.
- Call out any change that should be reviewed by humans before merging.

### 4) Optional Improved Wording
- Provide concrete rewrite suggestions for the top 3–5 issues.
- Keep wording concise and style-consistent with existing docs.

### 5) Final Quality Gate
Checklist:
- [ ] No unresolved contradictions remain
- [ ] Setup instructions are reproducible from scratch
- [ ] Agent instructions are deterministic and non-conflicting
- [ ] Security-sensitive guidance is safe and explicit

## Review Behavior Constraints
- Do not invent files, tools, or workflows not present in the repo.
- If context is missing, explicitly state what is missing rather than guessing.
- Distinguish objective defects from stylistic suggestions.
- Prioritize actionable, minimal-change recommendations.
