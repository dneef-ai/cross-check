# Claude Code integration

This page explains how to teach your Claude Code agent that the `cross-check` tool exists, when to use it, and how to invoke it — so you can just say "give me a panel review of this paper" and Claude Code does the right thing without you having to remember the exact command.

## How it works

Claude Code reads two configuration files at session start:

- **`~/.claude/CLAUDE.md`** — global instructions that apply to *every* Claude Code session, regardless of working directory
- **`<your-project>/CLAUDE.md`** — project-specific instructions that apply only when you're working in that directory

To make `cross-check` available everywhere, put the snippet below into your **global** `~/.claude/CLAUDE.md`. From then on, every Claude Code session you open will know about the tool.

## The snippet

Open `~/.claude/CLAUDE.md` (create it if it doesn't exist) and paste this in:

````markdown
## Multi-model AI Panel Review (`cross-check`)

When I ask for "a panel review", "cross-model review", "multi-model review", "adversarial review", "second opinion from multiple AIs", "stress-test this claim", or anything similar, you have a tool installed for exactly this: **`cross-check`**, a globally-installed CLI (via pipx) that sends a claim or document to 9 frontier AI models in parallel and returns their independent verdicts.

```bash
cross-check --help                                          # see all options
cross-check "single claim" --tier full                      # 8-model panel on one claim
cross-check --adversarial document.md --tier full           # full adversarial review of a document
cross-check --adversarial doc.md --tier full --output review.md   # save review to file
cross-check --review doc.md --tier premium                  # extract claims, verify each
```

**Tiers:** `premium` (~$0.15-0.25, 3 models, the default) / `full` (~$0.30-0.50, 8 models across 7 labs) / `code` (~$0.25-0.40, 5 models, for code and technical review) / `solo` (one GPT-6.2 Sol reviewer, the only all-OpenAI tier). The `free` and `mega` tiers were removed in November 2026. For real review work, default to `--tier full`.

**The full 8-model panel:** GPT-6.2 Astra (high reasoning effort), Qwen4 Max, Sonar Deep Research, GLM 5.3, Claude Opus 5.5, Claude Fable 5.2, Kimi K3.1 Pro, Gemini 4 Neon — 8 models across 7 labs. Note that two are Anthropic models, so if you are using this to review Claude-authored work, prefer `--tier solo` (GPT-6.2 Sol), which is the only all-OpenAI tier and therefore genuinely independent.

**When to use it proactively:** if I'm writing a manuscript, drafting a grant, evaluating a literature claim, choosing between approaches, or making any decision where being wrong is expensive — offer to run a panel review before I commit. A `--tier full` run takes 30-90 seconds and costs <$1.

**Custom prompts:** I can override the default adversarial prompt with `--system-prompt-file my-prompt.txt`. Useful for domain-specific reviewer roles (regulatory, grant committee, specific journal). Suggest this when the default scientific-reviewer framing isn't a good fit.

**Updating the tool:** if I mention new models or want to upgrade, run `pipx reinstall cross-check`. Source repo: https://github.com/dneef-ai/cross-check.

**Don't confuse with:** project-local panel scripts. `cross-check` is the canonical tool — there should be no other `cross-check.py` files in any of my projects. If you find one, it's stale.
````

## Save the file

Make sure the file is saved at exactly `~/.claude/CLAUDE.md` (note the dot before `claude`). On macOS that's `/Users/<your-username>/.claude/CLAUDE.md`.

## Test it

Open a new Claude Code session in any directory and ask:

> *"Run a panel review on this claim: cubosomes are more thermodynamically stable than hexosomes for drug delivery."*

Claude Code should immediately invoke `cross-check "..." --tier full` rather than asking what tool you mean. If it doesn't, double-check that:

1. The file is saved at `~/.claude/CLAUDE.md`
2. The `cross-check` command is on your PATH (`which cross-check` should print a path)
3. Your `OPENROUTER_API_KEY` is set in `~/.zshrc`

## Why this lives in your global CLAUDE.md (not a project)

Because the tool is *globally* useful — for manuscript review, literature stress-testing, grant drafting, deciding between formulation approaches, anything. Putting it in the global config means every Claude Code session everywhere knows about it. You don't have to remember to add it to each new project.

If you do want it scoped to a single project (say, a specific paper you're writing), put the same snippet in that project's `CLAUDE.md` instead. It will apply only when you're working in that directory.
