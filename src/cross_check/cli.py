#!/usr/bin/env python3
"""
Cross-check scientific claims against multiple AI models.
Usage:
    python3 cross-check.py "claim to verify"
    python3 cross-check.py --file claims.txt
    python3 cross-check.py --review /path/to/review.md          (extract claims + verify)
    python3 cross-check.py --adversarial /path/to/review.md     (full-document adversarial review)
    python3 cross-check.py --tier free|premium|full   (model tier, default: premium)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

MODEL_TIERS = {
    "free": {
        "description": "3-model free panel (Nemotron 3 Super 120B + GPT-OSS 120B + GLM 4.5 Air, all OpenRouter :free). Personal/non-Agteria use only (e.g. Meridian briefings). Reduced from 5 → 3 on 2026-05-20: OpenRouter per-minute rate limit always 429s the 4th-5th models in a parallel race regardless of which models are picked. Hermes + Llama + DeepSeek V4 Flash remain in code as explicit-call options.",
        "models": ["openrouter_nemotron_3_super_free", "openrouter_gpt_oss_120b_free",
                   "openrouter_glm_45_air_free"],
    },
    "premium": {
        "description": "Top 3 frontier (Gemini 3.1 Pro, GPT-5.5, Claude Opus 4.7)",
        "models": ["openrouter_gemini_31_pro", "openrouter_gpt55", "openrouter_claude_opus"],
    },
    "full": {
        "description": "5-model board panel (Gemini 3.1 Pro, GPT-5.5, o3-pro, Qwen 3.7 Max, Sonar Deep Research)",
        "models": ["openrouter_gemini_31_pro", "openrouter_gpt55", "openrouter_o3_pro",
                   "openrouter_qwen_37_max", "openrouter_sonar_deep_research"],
    },
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a veterinary pharmacology and microbiology expert reviewing claims about animal health therapeutics, pathogen biology, and drug discovery pipeline outputs.

For each claim, respond with EXACTLY this format:

VERDICT: AGREE / DISAGREE / PARTIALLY AGREE / UNCERTAIN
CONFIDENCE: HIGH / MEDIUM / LOW
REASONING: 1-3 sentences explaining your assessment.
EVIDENCE: Cite specific papers, facts, or mechanisms that support or contradict the claim. Include PMIDs if you know them.
ADDITIONS: Anything important the claim misses or gets wrong. Say "None" if the claim is complete."""

EXTRACT_PROMPT = """You are reviewing a scientific document about animal health drug discovery. Extract the 15-20 most important verifiable scientific claims. Prioritize:

1. MECHANISTIC CLAIMS that drive the document's conclusions (e.g., "Hla knockout eliminates lethality in murine mastitis")
2. EFFICACY CLAIMS with specific numbers (e.g., "DPC3147 achieved 47% cure rate vs 50% for antibiotics")
3. ABSENCE/NEGATIVE CLAIMS (e.g., "No isogenic delta-lukMF' bovine intramammary challenge has ever been published")
4. COMPARATIVE CLAIMS (e.g., "Bovine milk has 2000-5000x less lysozyme than human milk")
5. COMMERCIAL/REGULATORY CLAIMS (e.g., "Core patent WO2005034970A1 has expired across all jurisdictions")
6. CLAIMS THAT DRIVE RECOMMENDATIONS (e.g., "Sortase A has no mammalian homolog")

Each claim must be a single self-contained sentence that can be verified independently.
Do NOT extract opinions, recommendations, or subjective assessments — only verifiable factual claims.

Output a JSON array of strings. Example:
["Claim one.", "Claim two.", "Claim three."]

Output ONLY the JSON array. No markdown formatting. No explanation."""

ADVERSARIAL_PROMPT = """You are a senior R&D scientist at a major animal health company (Zoetis, Elanco, or equivalent). You have 15+ years of experience in veterinary drug development, deep expertise in the disease area covered by this review, and a low tolerance for hand-waving.

You are reading an external review of your company's AI-generated drug discovery pipeline output. Your job is to find issues that would CHANGE A DECISION — not to suggest hedging language or tone adjustments.

IMPORTANT RULES:
- Do NOT flag confidence language or tone. "Highly likely" vs "almost certainly" is not a finding. If the underlying evidence supports the conclusion, the conclusion stands regardless of how it's worded.
- Do NOT suggest adding caveats, hedges, or qualifications unless the underlying claim is actually wrong.
- Do NOT flag the absence of information that would not change any recommendation (e.g., "should mention milk residue testing" is only relevant if residue is a plausible concern for the specific intervention).
- EVERY finding must pass the "so what?" test: state explicitly what decision or recommendation would change if this finding is correct.

Structure your response as:

## FACTUAL ERRORS (Would change a recommendation)
[Claims that are demonstrably wrong. For each: quote the claim, state what is actually true, cite evidence, and state which recommendation would change. If you cannot name a specific recommendation that would change, do not include the finding.]

## CRITICAL MISSING EVIDENCE (Would add or kill a target)
[Information the review does not have that could flip a target from KEEP to KILL or vice versa. For each: state what the missing evidence is, where it might be found, and what the expected impact would be.]

## LOGIC ERRORS (Conclusion does not follow from evidence)
[Places where the review's reasoning is formally invalid — not just "could be more nuanced" but where the conclusion genuinely does not follow from the premises. State the premises, the conclusion, and what's wrong with the inference.]

## WHAT THE REVIEW GETS RIGHT
[The 3-5 strongest, most defensible claims. These are claims you would be comfortable presenting to your board without additional verification.]

## VERDICT
[One sentence: Is this review reliable enough to act on? If not, what single thing needs to be fixed first?]"""


def load_keys():
    """Load API keys from .zshrc and environment."""
    keys = {"gemini": "", "groq": "", "openrouter": "", "edison": ""}
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export GOOGLE_API_KEY="):
                    keys["gemini"] = line.split("=", 1)[1].strip('"').strip("'")
                elif line.startswith("export GROQ_API_KEY="):
                    keys["groq"] = line.split("=", 1)[1].strip('"').strip("'")
                elif line.startswith("export OPENROUTER_API_KEY="):
                    keys["openrouter"] = line.split("=", 1)[1].strip('"').strip("'")
                elif line.startswith("export EDISON_API_KEY="):
                    keys["edison"] = line.split("=", 1)[1].strip('"').strip("'")
    except FileNotFoundError:
        pass
    keys["gemini"] = keys["gemini"] or os.environ.get("GOOGLE_API_KEY", "")
    keys["groq"] = keys["groq"] or os.environ.get("GROQ_API_KEY", "")
    keys["openrouter"] = keys["openrouter"] or os.environ.get("OPENROUTER_API_KEY", "")
    keys["edison"] = keys["edison"] or os.environ.get("EDISON_API_KEY", "")
    return keys


# ---------------------------------------------------------------------------
# Model query functions
# ---------------------------------------------------------------------------

def query_gemini_flash(text, api_key, system_prompt=None, max_tokens=1000):
    """Query Google Gemini 2.5 Flash (free tier)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = f"{system_prompt}\n\n{text}" if system_prompt else text
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "cross-check/2.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            if result["candidates"][0].get("finishReason") == "MAX_TOKENS":
                text += "\n[TRUNCATED]"
            return "Gemini 2.5 Flash", text
    except Exception as e:
        return "Gemini 2.5 Flash", f"ERROR: {e}"


def query_groq(text, api_key, system_prompt=None, max_tokens=1000):
    """Query Groq (Llama 3.3 70B)."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "cross-check/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return "Llama 3.3 70B (Groq)", result["choices"][0]["message"]["content"]
    except Exception as e:
        return "Llama 3.3 70B (Groq)", f"ERROR: {e}"


def query_openrouter(text, api_key, model="openrouter/free", system_prompt=None, max_tokens=4000):
    """Query any model via OpenRouter."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "cross-check/2.0",
        "HTTP-Referer": "https://github.com/agteria/cross-check",
        "X-Title": "Agteria Cross-Check",
    })
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
            actual_model = result.get("model", model)
            short_model = actual_model.split("/")[-1].replace(":free", "")
            choice = result["choices"][0]
            message = choice.get("message", {})
            content = (message.get("content") or "").strip()
            # Reasoning models (DeepSeek V4, Kimi K2.6, o3-pro, etc.) emit hidden
            # chain-of-thought to `reasoning` and the final answer to `content`. If
            # max_tokens is exhausted by reasoning, content is None — fall back to
            # the reasoning text so the panel sees the model's thinking instead of
            # a silent empty.
            if not content:
                reasoning = (message.get("reasoning") or "").strip()
                if reasoning:
                    finish = choice.get("finish_reason", "?")
                    content = (
                        f"[content empty (finish_reason={finish}); "
                        f"falling back to chain-of-thought reasoning]\n\n{reasoning}"
                    )
            if not content:
                finish = choice.get("finish_reason", "?")
                content = f"ERROR: Empty response (finish_reason={finish})"
            return f"OpenRouter ({short_model})", content
    except Exception as e:
        return f"OpenRouter ({model.split('/')[-1]})", f"ERROR: {e}"


def query_edison(text, api_key, system_prompt=None, max_tokens=1000):
    """Query Edison Scientific platform (Future House PaperQA3) via edison-client.

    Edison is a specialized scientific research agent that retrieves and reads
    papers. It uses LITERATURE_HIGH job for adversarial reviews (high reasoning
    mode with SOTA performance). Unlike chat models, Edison actually searches
    and reads primary literature, producing cited responses.

    Note: Edison tasks can take 2-10 minutes. The client polls automatically.
    The max_tokens parameter is ignored — Edison controls its own output length.
    """
    try:
        from edison_client import EdisonClient, JobNames
    except ImportError:
        return "Edison (PaperQA3)", "ERROR: edison-client not installed. Run: pip install edison-client"

    query = f"{system_prompt}\n\n{text}" if system_prompt else text

    try:
        client = EdisonClient(api_key=api_key)
        task_response = client.run_tasks_until_done({
            "name": JobNames.LITERATURE_HIGH,
            "query": query,
        })
        answer = getattr(task_response, "formatted_answer", None) or getattr(task_response, "answer", "")
        if not answer:
            return "Edison (PaperQA3)", "ERROR: Empty response from Edison"
        return "Edison (PaperQA3)", answer
    except Exception as e:
        return "Edison (PaperQA3)", f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Model dispatcher — maps model ID to query function + args
# ---------------------------------------------------------------------------

def get_model_runner(model_id, keys):
    """Return (callable, display_name) for a model ID, or None if key missing."""
    if model_id == "gemini_flash":
        if not keys["gemini"]:
            return None
        return lambda text, sp=None, mt=4000: query_gemini_flash(text, keys["gemini"], sp, mt)
    elif model_id == "groq_llama":
        if not keys["groq"]:
            return None
        return lambda text, sp=None, mt=4000: query_groq(text, keys["groq"], sp, mt)
    elif model_id == "openrouter_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openrouter/free", sp, mt)
    elif model_id == "openrouter_gemini_31_pro":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-3.1-pro-preview", sp, mt)
    elif model_id == "openrouter_gpt55":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-5.5", sp, mt)
    elif model_id == "openrouter_grok_420_ma":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "x-ai/grok-4.20-multi-agent", sp, mt)
    elif model_id == "openrouter_claude_opus":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "anthropic/claude-opus-4.7", sp, mt)
    elif model_id == "openrouter_deepseek_v4":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "deepseek/deepseek-v4-pro", sp, mt)
    elif model_id == "openrouter_kimi_26":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "moonshotai/kimi-k2.6", sp, mt)
    elif model_id == "openrouter_o3_pro":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/o3-pro", sp, mt)
    elif model_id == "openrouter_qwen_37_max":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "qwen/qwen3.7-max", sp, mt)
    elif model_id == "openrouter_sonar_deep_research":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "perplexity/sonar-deep-research", sp, mt)
    elif model_id == "openrouter_deepseek_v4_flash_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "deepseek/deepseek-v4-flash:free", sp, mt)
    elif model_id == "openrouter_nemotron_3_super_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "nvidia/nemotron-3-super-120b-a12b:free", sp, mt)
    elif model_id == "openrouter_gpt_oss_120b_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-oss-120b:free", sp, mt)
    elif model_id == "openrouter_qwen3_next_80b_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "qwen/qwen3-next-80b-a3b-instruct:free", sp, mt)
    elif model_id == "openrouter_glm_45_air_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "z-ai/glm-4.5-air:free", sp, mt)
    elif model_id == "openrouter_hermes3_405b_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "nousresearch/hermes-3-llama-3.1-405b:free", sp, mt)
    elif model_id == "openrouter_llama_33_70b_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "meta-llama/llama-3.3-70b-instruct:free", sp, mt)
    elif model_id == "edison":
        if not keys["edison"]:
            return None
        return lambda text, sp=None, mt=4000: query_edison(text, keys["edison"], sp, mt)
    else:
        return None


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def extract_claims(review_text, keys, tier="premium"):
    """Extract key claims from a review document using the best available model."""
    # Use Gemini Pro via OpenRouter for standard/premium, Gemini Flash for free
    if tier in ("premium", "full") and keys["openrouter"]:
        print("Extracting claims with Gemini 3.1 Pro Preview...")
        _, result = query_openrouter(
            f"{EXTRACT_PROMPT}\n\n---\n\n{review_text}",
            keys["openrouter"],
            model="google/gemini-3.1-pro-preview",
            max_tokens=3000,
        )
    elif keys["gemini"]:
        print("Extracting claims with Gemini Flash...")
        _, result = query_gemini_flash(
            f"{EXTRACT_PROMPT}\n\n---\n\n{review_text}",
            keys["gemini"],
            max_tokens=3000,
        )
    else:
        print("ERROR: No model available for claim extraction", file=sys.stderr)
        return []

    if result.startswith("ERROR:"):
        print(f"Extraction failed: {result}", file=sys.stderr)
        return []

    text = result.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        claims = []
        for line in text.split("\n"):
            line = line.strip().strip("-•*").strip().strip('"').strip("'").strip(",")
            if len(line) > 20 and not line.startswith("[") and not line.startswith("{"):
                claims.append(line)
        return claims[:20]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_result(model_name, response):
    """Format a model's response for display."""
    width = 76
    lines = [f"  {model_name}", "  " + "=" * len(model_name)]
    for line in response.strip().split("\n"):
        wrapped = textwrap.fill(line, width=width, initial_indent="  ", subsequent_indent="    ")
        lines.append(wrapped)
    return "\n".join(lines)


def format_result_markdown(model_name, response):
    """Format a model's response as markdown for file output."""
    return f"### {model_name}\n\n{response.strip()}\n"


# ---------------------------------------------------------------------------
# Claim checking
# ---------------------------------------------------------------------------

def check_claim(claim, runners, claim_num=None):
    """Check a single claim against all available models in parallel."""
    header = f"CLAIM {claim_num}" if claim_num else "CLAIM"
    print(f"\n{'='*76}")
    print(f"  {header}: {claim[:100]}{'...' if len(claim) > 100 else ''}")
    print(f"{'='*76}")

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(runners), 6)) as executor:
        futures = {}
        for runner in runners:
            futures[executor.submit(runner, f"CLAIM TO VERIFY:\n{claim}", SYSTEM_PROMPT)] = True

        for future in as_completed(futures):
            model_name, response = future.result()
            results[model_name] = response

    for model, response in results.items():
        if response is None:
            response = "ERROR: No response returned (None)"
            results[model] = response
        print(f"\n{format_result(model, response)}")

    # Check for disagreements
    verdicts = {}
    for model, response in results.items():
        if response.startswith("ERROR:"):
            continue
        for line in response.split("\n"):
            if line.strip().upper().startswith("VERDICT:"):
                verdicts[model] = line.strip().split(":", 1)[1].strip().upper()
                break

    agree_count = sum(1 for v in verdicts.values() if "AGREE" in v and "DISAGREE" not in v)
    disagree_count = sum(1 for v in verdicts.values() if "DISAGREE" in v)
    error_count = sum(1 for r in results.values() if r.startswith("ERROR:"))

    if disagree_count > 0:
        disagree_models = [m for m, v in verdicts.items() if "DISAGREE" in v]
        print(f"\n  *** DISAGREEMENT: {', '.join(disagree_models)} ***")
    elif agree_count == len(verdicts) and len(verdicts) > 1:
        print(f"\n  All {len(verdicts)} models agree.")

    if error_count > 0:
        print(f"  ({error_count} model(s) unavailable)")

    print()
    return results


# ---------------------------------------------------------------------------
# Adversarial review
# ---------------------------------------------------------------------------

def adversarial_review(review_path, runners, output_path=None, system_prompt=None):
    """Send full document to each model for adversarial review."""
    prompt = system_prompt or ADVERSARIAL_PROMPT
    with open(review_path) as f:
        review_text = f.read()

    print(f"\nAdversarial review of: {review_path}")
    print(f"Document length: {len(review_text):,} chars, {len(review_text.splitlines()):,} lines")
    print(f"Models: {len(runners)}")
    if system_prompt:
        print(f"Using custom system prompt ({len(system_prompt):,} chars)")
    print(f"{'='*76}\n")

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(runners), 6)) as executor:
        futures = {}
        for runner in runners:
            futures[executor.submit(
                runner,
                f"REVIEW DOCUMENT TO ANALYZE:\n\n{review_text}",
                prompt,
                4000,
            )] = True

        for future in as_completed(futures):
            model_name, response = future.result()
            results[model_name] = response
            if response is None:
                response = "ERROR: No response returned"
                results[model_name] = response
            print(f"\n{format_result(model_name, response)}")
            print()

    # Save to file if output path specified
    if output_path:
        with open(output_path, "w") as f:
            f.write(f"# Adversarial Review — {os.path.basename(review_path)}\n\n")
            f.write(f"Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(f"Document: `{review_path}`\n\n---\n\n")
            for model, response in results.items():
                f.write(format_result_markdown(model, response))
                f.write("\n---\n\n")
        print(f"\nSaved to: {output_path}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cross-check scientific claims against multiple AI models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Model tiers:
              free        3-model free panel: Nemotron 3 Super 120B + GPT-OSS 120B + GLM 4.5 Air (all OpenRouter :free). Use for personal/non-Agteria work (e.g. Meridian briefings).
              premium     Gemini 3.1 Pro + GPT-5.5 + Claude Opus 4.7 (default). Use for routine cross-check work.
              full        5-model panel: Gemini 3.1 Pro + GPT-5.5 + o3-pro + Qwen 3.7 Max + Sonar Deep Research. Default for research-pipeline panel reviews (Atlas, Argus, Auditor, Zero-Day, Alchemist, watchtower).
        """),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("claim", nargs="?", help="A single claim to verify")
    group.add_argument("--file", "-f", help="File with one claim per line")
    group.add_argument("--review", "-r", help="Review markdown file (auto-extracts claims)")
    group.add_argument("--adversarial", "-a", help="Full-document adversarial review")
    parser.add_argument("--tier", "-t", choices=["free", "premium", "full"],
                        default="premium", help="Model tier (default: premium)")
    parser.add_argument("--output", "-o", help="Output file for adversarial review")
    parser.add_argument("--system-prompt-file", help="Custom system prompt file (overrides built-in adversarial prompt)")
    args = parser.parse_args()

    keys = load_keys()
    tier = args.tier
    tier_info = MODEL_TIERS[tier]

    # Build runners for this tier
    runners = []
    available_names = []
    for model_id in tier_info["models"]:
        runner = get_model_runner(model_id, keys)
        if runner:
            runners.append(runner)
            available_names.append(model_id)

    if not runners:
        print("ERROR: No API keys found for the selected tier.", file=sys.stderr)
        print(f"Tier '{tier}' needs: {tier_info['models']}", file=sys.stderr)
        sys.exit(1)

    print(f"Tier: {tier} — {tier_info['description']}")
    print(f"Active models: {', '.join(available_names)}")
    print(f"{'='*76}")

    if args.adversarial:
        output = args.output
        if not output:
            base = os.path.splitext(args.adversarial)[0]
            output = f"{base}-adversarial.md"
        custom_prompt = None
        if args.system_prompt_file:
            with open(args.system_prompt_file) as pf:
                custom_prompt = pf.read()
        adversarial_review(args.adversarial, runners, output, system_prompt=custom_prompt)

    elif args.review:
        print(f"Extracting claims from: {args.review}")
        with open(args.review) as f:
            review_text = f.read()
        claims = extract_claims(review_text, keys, tier)
        print(f"Extracted {len(claims)} claims to verify.\n")
        for i, claim in enumerate(claims, 1):
            check_claim(claim, runners, claim_num=i)

    elif args.file:
        with open(args.file) as f:
            claims = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        for i, claim in enumerate(claims, 1):
            check_claim(claim, runners, claim_num=i)

    else:
        check_claim(args.claim, runners)


if __name__ == "__main__":
    main()
