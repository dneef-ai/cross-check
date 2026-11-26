# cross-check

A multi-model adversarial peer review panel for scientific claims and documents. Sends your claim or document to several frontier AI models in parallel via OpenRouter, then surfaces where they agree and where they disagree.

Useful for: stress-testing a hypothesis, sanity-checking a manuscript before submission, getting independent second opinions on a literature claim, or finding the weakest part of a research plan before you commit to it.

## What it does

Send one claim, get independent verdicts from each model on the panel. Each model gives reasoning, evidence (with PMIDs/DOIs when known), and additions. Disagreements are highlighted automatically.

You can also feed it a whole document for full adversarial review — it returns structured "factual errors / missing evidence / logic errors / what's right / verdict" per model.

## Install (one time)

You need [pipx](https://pipx.pypa.io/) installed (`brew install pipx` on Mac).

```bash
pipx install git+https://github.com/dneef-ai/cross-check.git
```

That's it. The `cross-check` command is now available globally from any directory.

## Set up your API key (one time)

You need an [OpenRouter](https://openrouter.ai) account with credits. Generate an API key in your dashboard, then add it to your shell config:

```bash
echo 'export OPENROUTER_API_KEY="sk-or-v1-xxxxxx"' >> ~/.zshrc
source ~/.zshrc
```

(Optional: `GOOGLE_API_KEY` enables a Gemini Flash fallback for claim extraction when `--review` is used with a tier other than `premium`/`full`.)

## Use it

**Single claim:**
```bash
cross-check "Phytantriol-based cubosomes show better encapsulation efficiency than Pluronic F-127 stabilized particles for hydrophobic drugs" --tier full
```

**A whole document:**
```bash
cross-check --adversarial my-manuscript.md --tier full --output review.md
```

**Extract claims from a doc, then verify each:**
```bash
cross-check --review my-analysis.md --tier premium
```

**See all options:**
```bash
cross-check --help
```

## Tiers

| Tier | Models | Approx cost / run | When to use |
|---|---|---|---|
| `premium` (default) | GPT-6.2 Astra (high) + Gemini 4 Neon + Claude Fable 5.2 | ~$0.15-0.25 | Routine cross-checking. The cheapest panel |
| `full` | 8 models across 7 labs | ~$0.30-0.50 | Critical decisions, manuscript reviews, thorough document review |
| `code` | 5 models across 5 labs | ~$0.25-0.40 | Code review and scientific/technical review |
| `code5` | deprecated alias for `code` | same as `code` | Legacy callers only |
| `solo` | GPT-6.2 Sol only | cheapest per call | A single reviewer independent of a Claude-based author |

**The `full` panel:** GPT-6.2 Astra (high), Qwen4 Max, Sonar Deep Research, GLM 5.3, Claude Opus 5.5, Claude Fable 5.2, Kimi K3.1 Pro, Gemini 4 Neon. Two of the eight are Anthropic models, so it spans 7 labs rather than 8 — worth knowing if you are using it to review work an Anthropic model wrote. Sonar Deep Research often answers with a literature essay and no `VERDICT:` line; it is counted as "did not answer" rather than quietly dropped, so this tier frequently has an effective n of 7.

**The `code` panel:** Kimi K3.1 Pro, GPT-6.2 Astra (high), Qwen4 Max, GLM 5.3, Claude Fable 5.2. Pass your own `--system-prompt-file` — the built-in adversarial prompt is written for veterinary pharmacology and is a poor fit for code.

**Independence matters.** A reviewer from the same model family as the author tends to share its blind spots and re-confirm its reasoning. `solo` is the only all-OpenAI tier, which is why it is the one to reach for when reviewing output an Anthropic model produced. `full`, `code` and `premium` all include at least one Anthropic model.

**Removed in November 2026:** the `free` tier (its OpenRouter `:free` models were delisted upstream, leaving the panel running on a single model) and the `mega` tier (a 15-model strategic panel that was not adding verdicts over `full` in practice). There is no zero-cost tier; `premium` is the floor for a panel.

**A caution on citations.** Several frontier models, across labs, will cite a real PMID that resolves to an unrelated paper. Treat any PMID a panel gives you as unverified until you check it.


## Update to the latest version

When new models drop or prompts get refined:

```bash
pipx reinstall cross-check
```

## Custom prompts

Override the built-in adversarial prompt with your own:

```bash
cross-check --adversarial doc.md --tier full --system-prompt-file my-prompt.txt
```

Useful when you want the panel to play a specific reviewer role (e.g., a regulatory reviewer, a grant committee, a specific journal's standards).

## Using with Claude Code

If you use [Claude Code](https://claude.com/claude-code), paste the snippet from [`docs/claude-code-integration.md`](docs/claude-code-integration.md) into your global `~/.claude/CLAUDE.md` so Claude Code knows about this tool and uses it automatically whenever you ask for a panel review.

## License

MIT
