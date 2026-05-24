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

(Optional: also `GOOGLE_API_KEY` for Gemini Flash and `GROQ_API_KEY` for Llama, used by the free tier.)

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
| `free` | 3-model OpenRouter free panel (Nemotron 3 Super 120B + GPT-OSS 120B + GLM 4.5 Air) | $0 | Quick sanity check; personal-use work |
| `premium` (default) | Gemini 3.1 Pro + GPT-5.5 + Claude Opus 4.7 | ~$0.15 | Routine cross-check work |
| `full` | **5-model board panel** | ~$0.30-0.50 | Critical decisions, manuscript reviews, research-pipeline panels |

The full panel: Gemini 3.1 Pro, GPT-5.5, o3-pro, Qwen 3.7 Max, and Sonar Deep Research. The board was trimmed from 9 → 5 models on 2026-05-20 after OpenRouter's per-minute rate limit started 429'ing the 4th-5th models in a parallel race regardless of which models were chosen. The 5-model panel is the honest panel — every slot reliably responds.

The `free` panel was consolidated from two near-identical free tiers on 2026-05-25 — same effective behavior, simpler API.

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
