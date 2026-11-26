#!/usr/bin/env python3
"""
Cross-check scientific claims against multiple AI models.
Usage:
    python3 cross-check.py "claim to verify"
    python3 cross-check.py --file claims.txt
    python3 cross-check.py --review /path/to/review.md          (extract claims + verify)
    python3 cross-check.py --adversarial /path/to/review.md     (full-document adversarial review)
    cross-check --tier premium|full|solo|code|code5   (model tier, default: premium)
"""

import argparse
import datetime
import email.utils
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
    "premium": {
        "description": "3-model frontier panel: GPT-6.2 Astra (high reasoning effort) + Gemini 4 Neon + Claude Fable 5.2. The default tier and the cheapest panel; roughly $0.15-0.25 per claim. Use for routine cross-checking.",
        "models": ["openrouter_astra_62_high", "openrouter_gemini_4_neon", "openrouter_fable_52"],
    },
    "full": {
        "description": "8-model board panel across 7 labs: GPT-6.2 Astra (high), Qwen4 Max, Sonar Deep Research, GLM 5.3, Claude Opus 5.5, Claude Fable 5.2, Kimi K3.1 Pro, Gemini 4 Neon. Roughly $0.30-0.50 per claim. Use for thorough document and research review where breadth of opinion matters. Two of the eight are Anthropic models, so the panel spans 7 labs rather than 8 and is correspondingly less independent of a Claude-based author -- use the `solo` tier when you specifically want a non-Anthropic reviewer. Note that Sonar Deep Research often returns a literature essay without a VERDICT line; it is tallied as 'did not answer' rather than silently dropped, so this tier frequently has an effective n of 7.",
        "models": ["openrouter_astra_62_high", "openrouter_qwen4_max",
                   "openrouter_sonar_deep_research", "openrouter_glm_53",
                   "openrouter_claude_opus", "openrouter_fable_52",
                   "openrouter_kimi_k31_pro", "openrouter_gemini_4_neon"],
    },
    "solo": {
        "description": "A single GPT-6.2 Sol reviewer. The only all-OpenAI tier, which makes it the right choice when you want a reviewer that is independent of a Claude-based author (for example, reviewing code an Anthropic model wrote). Cheapest per call, but it is one voice, not a panel -- don't substitute it for a panel just to save money. Note that GPT-5.x/6.x models can cite real PMIDs for the wrong papers, so verify citations independently.",
        "models": ["openrouter_gpt62sol"],
    },
    "code": {
        "description": "5-model coding and scientific review panel across 5 labs: Kimi K3.1 Pro, GPT-6.2 Astra (high), Qwen4 Max, GLM 5.3, Claude Fable 5.2 (Moonshot/OpenAI/Alibaba/Z-AI/Anthropic). Pass a code-appropriate --system-prompt-file: the built-in adversarial prompt is written for veterinary pharmacology. Includes an Anthropic model, so it is not independent of a Claude-based author -- use `solo` for that.",
        "models": ["openrouter_kimi_k31_pro", "openrouter_astra_62_high", "openrouter_qwen4_max", "openrouter_glm_53", "openrouter_fable_52"],
    },
    "code5": {
        "description": "Deprecated alias with the same roster as `code`. Kept so existing scripts keep working; prefer `code`.",
        "models": ["openrouter_kimi_k31_pro", "openrouter_astra_62_high", "openrouter_qwen4_max", "openrouter_glm_53", "openrouter_fable_52"],
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


# Free-tier OpenRouter endpoints rate-limit aggressively (HTTP 429), and a panel
# fires every member concurrently — without a retry a transient 429 silently drops
# that model and the run reports a smaller board than configured.
#
# ONLY 429 is retried, deliberately. A 5xx or a dropped connection is ambiguous: the
# request may already have reached the model, so the generation is billed whether or
# not we see the response. Re-POSTing it would pay for the same generation two or
# three times — on the mega tier that is a $10/$50-per-Mtok model. A 429, by
# contrast, means the request was refused before it ran, so replaying it is free and
# safe. Everything else fails fast and is reported as unavailable.
_HTTP_MAX_ATTEMPTS = 3
_HTTP_BACKOFF_BASE = 2.0   # seconds; doubled each attempt
_HTTP_BACKOFF_CAP = 30.0   # we will not sleep longer than this inside a panel worker


def _retry_after_seconds(exc):
    """Delay the server asked for via Retry-After, or None if it did not ask.

    RFC 9110 permits two forms — delta-seconds and an HTTP-date — and providers use
    both. Parsing only the numeric form made an HTTP-date fall back to the local
    backoff, which retried inside the rate-limit window and burned the attempt.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    raw = (headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    # delta-seconds is digits only (RFC 9110). float() would also swallow "1e2",
    # "NaN" and "inf" — and max(0.0, nan) returns 0.0, i.e. an instant zero-delay
    # retry straight back into the same rate-limit window.
    if raw.isascii() and raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def _urlopen_with_retry(req, timeout=300):
    """urlopen, retrying only HTTP 429. Raises the last error once attempts run out."""
    for attempt in range(_HTTP_MAX_ATTEMPTS):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == _HTTP_MAX_ATTEMPTS - 1:
                raise
            requested = _retry_after_seconds(exc)
            delay = requested if requested is not None else _HTTP_BACKOFF_BASE * (2 ** attempt)
            # Never retry earlier than the server asked; if its window is longer than
            # a panel worker should block for, give up rather than retry into another 429.
            if delay > _HTTP_BACKOFF_CAP:
                raise
            # Release the error response's socket before sleeping — a 15-worker mega
            # panel would otherwise hold them all open for the whole backoff.
            exc.close()
            time.sleep(delay)


def query_openrouter(text, api_key, model="openrouter/free", system_prompt=None, max_tokens=4000, reasoning_effort=None):
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
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "cross-check/2.0",
        "HTTP-Referer": "https://github.com/dneef-ai/cross-check",
        "X-Title": "Cross-Check",
    })
    try:
        with _urlopen_with_retry(req) as resp:
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
    elif model_id == "openrouter_gemini_35_flash":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-3.5-flash", sp, mt)
    elif model_id == "openrouter_gemini_37_flash":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-3.7-flash", sp, mt)
    elif model_id == "openrouter_gemini_38_flash":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-3.8-flash", sp, mt)
    elif model_id == "openrouter_gemini_4_argon":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-4-argon", sp, mt)
    elif model_id == "openrouter_gpt55":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-5.5", sp, mt)
    elif model_id == "openrouter_gpt55_xhigh":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-5.5", sp, mt, reasoning_effort="xhigh")
    elif model_id == "openrouter_gpt56sol":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-5.6-sol", sp, mt)
    elif model_id == "openrouter_gpt56sol_xhigh":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-5.6-sol", sp, mt, reasoning_effort="xhigh")
    elif model_id == "openrouter_gpt62sol":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-6.2-sol", sp, mt)
    elif model_id == "openrouter_gpt62sol_xhigh":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-6.2-sol", sp, mt, reasoning_effort="xhigh")
    elif model_id == "openrouter_astra_high":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-6-astra", sp, mt, reasoning_effort="high")
    elif model_id == "openrouter_astrapro_high":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-6-astra-pro", sp, mt, reasoning_effort="high")
    elif model_id == "openrouter_astra_62_high":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "openai/gpt-6.2-astra", sp, mt, reasoning_effort="high")
    elif model_id == "openrouter_fable_52":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "anthropic/claude-fable-5.2", sp, mt)
    elif model_id == "openrouter_qwen4_max":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "qwen/qwen4-max", sp, mt)
    elif model_id == "openrouter_gemini_4_neon":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemini-4-neon", sp, mt)
    elif model_id == "openrouter_claude_opus":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "anthropic/claude-opus-5.5", sp, mt)
    elif model_id == "openrouter_claude_opus_max":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "anthropic/claude-opus-5.5", sp, mt, reasoning_effort="xhigh")
    elif model_id == "openrouter_fable_5":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "anthropic/claude-fable-5", sp, mt, reasoning_effort="xhigh")
    elif model_id == "openrouter_kimi_26":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "moonshotai/kimi-k2.6", sp, mt)
    elif model_id == "openrouter_kimi_k3":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "moonshotai/kimi-k3", sp, mt)
    elif model_id == "openrouter_qwen_38_max":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "qwen/qwen3.8-max", sp, mt)
    elif model_id == "openrouter_qwen_38_max_0902":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "qwen/qwen3.8-max-0902", sp, mt)
    elif model_id == "openrouter_kimi_k31_pro":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "moonshotai/kimi-k3.1-pro", sp, mt)
    elif model_id == "openrouter_sonar_deep_research":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "perplexity/sonar-deep-research", sp, mt)
    elif model_id == "openrouter_glm_51":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "z-ai/glm-5.1", sp, mt)
    elif model_id == "openrouter_glm_52":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "z-ai/glm-5.2", sp, mt)
    elif model_id == "openrouter_glm_53":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "z-ai/glm-5.3", sp, mt)
    elif model_id == "openrouter_mimo_25_pro":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "xiaomi/mimo-v2.5-pro", sp, mt)
    elif model_id == "openrouter_mimo_26_pro":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "xiaomi/mimo-v2.6-pro", sp, mt)
    elif model_id == "openrouter_ring_26_1t":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "inclusionai/ring-2.6-1t", sp, mt)
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
    elif model_id == "openrouter_gemma_4_31b_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "google/gemma-4-31b-it:free", sp, mt)
    elif model_id == "openrouter_nemotron_35_super_vl_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "nvidia/nemotron-3.5-super-vl:free", sp, mt)
    elif model_id == "openrouter_north_mini_code_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "cohere/north-mini-code:free", sp, mt)
    elif model_id == "openrouter_dots3_note_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "dots-studio/dots-3-note-preview:free", sp, mt)
    elif model_id == "openrouter_nemotron_3_ultra_free":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "nvidia/nemotron-3-ultra-550b-a55b:free", sp, mt)
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
    elif model_id == "openrouter_grok_4":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "x-ai/grok-4.7", sp, mt)
    elif model_id == "openrouter_deepseek_v4":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "deepseek/deepseek-v4-pro", sp, mt)
    elif model_id == "openrouter_mistral_large":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "mistralai/mistral-large-2512", sp, mt)
    elif model_id == "openrouter_mistral_large_4":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "mistralai/mistral-large-4-0", sp, mt)
    elif model_id == "openrouter_llama4":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "meta-llama/llama-4-maverick", sp, mt)
    elif model_id == "openrouter_minimax_m3":
        if not keys["openrouter"]:
            return None
        return lambda text, sp=None, mt=4000: query_openrouter(text, keys["openrouter"], "minimax/minimax-m3", sp, mt)
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

# Models format the verdict label inconsistently. Observed in real runs:
#   "VERDICT: AGREE"            (most)
#   "**VERDICT:** PARTIALLY AGREE"   (claude-opus-5, 2026-09-05 mega)
# The original parser used str.startswith("VERDICT:"), so a markdown-emphasised
# label failed to match and that model was dropped from the tally entirely —
# counted as neither agreement, disagreement nor error. On a 16-model mega run
# that printed "All 13 models agree" while three models had actually qualified
# their agreement and one had vanished. Be liberal about the label; be strict
# about reporting anything we still cannot classify.
# Character classes are kept disjoint from what follows them so the pattern
# stays linear. An earlier draft had `[\s>*\-#]*\**`, whose two quantifiers both
# matched "*"; a 20k-asterisk line took 5.7s of backtracking.
# All whitespace here is HORIZONTAL only — "\s" matches newlines, which let the
# pattern skip an empty "VERDICT:" line and capture the following prose instead.
_LEAD = r"[ \t>*#`_\-.\d)\]]"      # bullets, numbering, emphasis, blockquote
_EMPH = r"[ \t*`_]"                # emphasis/space around the label and value
_SEP = r"[:\-–—]"
VERDICT_RE = re.compile(
    rf"^{_LEAD}*VERDICT{_EMPH}*{_SEP}{_EMPH}*"
    rf"([^\s*`_][^\r\n]*?)"        # value: starts non-space/non-emphasis, stays on the line
    rf"{_EMPH}*$",
    re.IGNORECASE | re.MULTILINE,
)

# query_openrouter substitutes this PREFIX when a model burns its budget on
# reasoning and returns empty content. Matched with startswith, not `in`:
# a model quoting the marker in its own prose must not be mislabelled truncated.
TRUNCATION_PREFIX = "[content empty (finish_reason="

# Word-bounded so "AGREEMENT" does not count as "AGREE" and "UNSUPPORTED" does
# not count as "SUPPORTED". Substring matching classified "NOT AGREE" as
# agreement.
# Negation must cover EVERY polarity word, not just AGREE: with only AGREE
# negated, "VERDICT: NOT SUPPORTED" matched the positive word SUPPORTED and was
# reported as agreement — a false-unanimity path.
_NEG = r"(?:NOT|CANNOT|CAN'?T|DO(?:ES)? ?N'?T|IS ?N'?T|UNABLE TO|NO)"
_POSITIVE = r"(?:AGREES?|AGREED|SUPPORTED|CORRECT|TRUE|VALID)"
_NEGATIVE = r"(?:DISAGREES?|REFUTED?|FALSE|INCORRECT|REJECTED?|UNSUPPORTED|INVALID)"

# Polarity is decided by the FIRST polarity word in the value, with a lookback
# for a negator in front of it. Deciding it per comma-separated segment was
# wrong: "The claim cannot, in its current form, be supported" split into
# ["The claim cannot", "in its current form", "be supported"], the negator was
# stranded in a segment of its own, and the verdict came out as agreement.
_POLARITY_TOKEN_RE = re.compile(rf"\b(?P<pos>{_POSITIVE})\b|\b(?P<neg>{_NEGATIVE})\b", re.I)
# A negator counts if it precedes the polarity word with no sentence break
# between them; the window absorbs interposed clauses like ", in its current form,".
_NEGATOR_TAIL_RE = re.compile(rf"\b{_NEG}\b[^.;]{{0,40}}$", re.I)

_PARTIAL_RE = re.compile(
    r"\bPARTIAL|\bQUALIFIED\b|\bMIXED\b|\bMOSTLY\b|\bLARGELY\b|\bGENERALLY\b|\bBROADLY\b"
    r"|\bSUBSTANTIALLY\b|\bIN PART\b|\bWITH CAVEATS?\b|\bWITH RESERVATIONS?\b", re.I)

# A verdict value is split into segments so that a model's inline justification
# cannot change its polarity: "AGREE - the contrary interpretation is false"
# must stay agreement, not be flipped by the word "false" in the explanation.
_SEGMENT_SPLIT_RE = re.compile(r"[—–,;:.()\[\]]|\s+-\s+")

# A later segment only qualifies the verdict if it is grammatically attached to
# it. "AGREE, but with reservations" qualifies; "AGREE - unlike the opposing
# view, which is only partially supported" does not.
_QUALIFIER_LEAD_RE = re.compile(
    r"^(?:BUT|HOWEVER|THOUGH|ALTHOUGH|ALBEIT|WITH|SUBJECT TO|CAVEAT)\b", re.I)
# "with no caveats" opens with a qualifier connective but denies the caveat.
_NEGATED_QUALIFIER_RE = re.compile(r"^\w+(?:\s+TO)?\s+(?:NO|NONE|NOT)\b", re.I)


def _segments(value):
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(value) if s.strip()]


def _polarity(value):
    """Resolve the value's polarity from its first polarity word, or None."""
    match = _POLARITY_TOKEN_RE.search(value)
    if not match:
        return None
    negated = bool(_NEGATOR_TAIL_RE.search(value[:match.start()]))
    if match.group("pos"):
        return "disagree" if negated else "agree"
    return "agree" if negated else "disagree"


def _is_qualified(value):
    """True when the agreement carries a caveat.

    Scoped so a qualifier describing someone else's position does not downgrade
    our own: only the segment holding the polarity word counts, plus later
    segments that open with a qualifier connective and do not negate it.
    """
    segments = _segments(value) or [value]
    for index, segment in enumerate(segments):
        if _POLARITY_TOKEN_RE.search(segment):
            if _PARTIAL_RE.search(segment):
                return True
            return any(_QUALIFIER_LEAD_RE.match(s) and not _NEGATED_QUALIFIER_RE.match(s)
                       for s in segments[index + 1:])
    return bool(_PARTIAL_RE.search(value))


def classify_verdict(raw):
    """Bucket a verdict string into agree / partial / disagree / unclear.

    Polarity comes from the first polarity word in the value, with a lookback for
    a preceding negator, so "AGREE, the opposing claim is incorrect" stays
    agreement (the verdict word leads) while "The claim cannot, in its current
    form, be supported" is disagreement (the negator governs the later word).

    Qualification is then scoped by segment, so "AGREE, but with reservations" is
    `partial`, "AGREE, with no caveats" is `agree`, and a caveat describing
    someone else's position does not downgrade ours.

    Dissent is never softened: any negated positive ("NOT AGREE",
    "NOT SUPPORTED") is `disagree`, and "PARTIALLY DISAGREE" is disagreement
    rather than partial. Anything not positively recognised falls to `unclear`,
    which is reported loudly rather than folded into agreement.
    """
    value = " ".join(raw.split())
    polarity = _polarity(value)
    if polarity == "disagree":
        return "disagree"
    if polarity == "agree":
        return "partial" if _is_qualified(value) else "agree"
    return "partial" if _PARTIAL_RE.search(value) else "unclear"


def extract_verdict(response):
    """Return the model's verdict text, or None if it never gave one.

    Uses the LAST verdict line, unconditionally. Two earlier policies were both
    wrong: taking the FIRST match recorded a restated format example instead of
    the answer, and taking the last *classifiable* match skipped a final
    correction such as "VERDICT: ABSTAIN" and returned the stale earlier verdict.

    A previous revision also filtered out candidates containing a backtick or
    longer than 120 characters, to ignore quoted counter-examples. That was
    removed: it discarded legitimate corrections such as
    "VERDICT: DISAGREE - the `is_admin` check is bypassable", silently restoring
    an earlier AGREE and reporting false unanimity. Suppressing a real dissent is
    a worse failure than the one the filter prevented.

    Residual limitation: a model that states its verdict and then writes another
    verdict-shaped line (a quoted counter-example) will have the later line taken
    as its answer. That ordering is inherently ambiguous from text alone, and the
    failure is loud — it surfaces as a disagreement banner, not a silent drop.
    """
    matches = [m.group(1).strip() for m in VERDICT_RE.finditer(response)]
    return matches[-1] if matches else None


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

    # Classify every response. Each model lands in exactly one bucket, and every
    # bucket is reported — a response must never disappear from the tally.
    verdicts, unparsed, truncated, errored = {}, [], [], []
    for model, response in results.items():
        if response.startswith("ERROR:"):
            errored.append(model)
            continue
        raw = extract_verdict(response)
        if raw is None:
            (truncated if response.startswith(TRUNCATION_PREFIX) else unparsed).append(model)
            continue
        verdicts[model] = classify_verdict(raw)

    agree = [m for m, k in verdicts.items() if k == "agree"]
    partial = [m for m, k in verdicts.items() if k == "partial"]
    disagree = [m for m, k in verdicts.items() if k == "disagree"]
    unclear = [m for m, k in verdicts.items() if k == "unclear"]

    if disagree:
        print(f"\n  *** DISAGREEMENT: {', '.join(disagree)} ***")
    if partial:
        print(f"  *** QUALIFIED ({len(partial)}): {', '.join(partial)} ***")
    if unclear:
        print(f"  *** UNCLEAR VERDICT ({len(unclear)}): {', '.join(unclear)} ***")

    # "All N models agree" is only honest when nothing else happened. A run with
    # six agreements plus a truncation and a non-answer is NOT unanimity, and
    # saying so was the original defect.
    unanswered = unparsed + truncated + errored
    if agree and not (disagree or partial or unclear or unanswered) and len(agree) > 1:
        print(f"\n  All {len(agree)} models agree.")
    elif verdicts:
        summary = (f"\n  Verdicts: {len(agree)} agree, {len(partial)} partial, "
                   f"{len(disagree)} disagree, {len(unclear)} unclear")
        if unanswered:
            summary += f" — {len(unanswered)} did not answer"
        print(summary)
    else:
        print(f"\n  No parseable verdict from any of the {len(results)} models.")

    if errored:
        print(f"  ({len(errored)} model(s) unavailable: {', '.join(errored)})")
    if truncated:
        print(f"  ({len(truncated)} model(s) truncated before answering: {', '.join(truncated)})")
    if unparsed:
        print(f"  ({len(unparsed)} model(s) returned no VERDICT line: {', '.join(unparsed)})")

    accounted = len(verdicts) + len(unparsed) + len(truncated) + len(errored)
    if accounted != len(results):
        print(f"  !! TALLY BUG: {accounted} classified but {len(results)} responded")
    print(f"  [{accounted}/{len(results)} models accounted for]")

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
    # max_tokens=24000 for adversarial mode: models like GPT-5.5 and Gemini consume
    # 3000-6000 tokens on internal reasoning before producing content. A 4000 cap
    # truncates them to empty/CoT-fallback responses on multi-item benchmarks or
    # long documents. 12000 still left Gemini truncating at item 5/12; 24000 gives
    # enough headroom for the most reasoning-heavy models without changing actual
    # cost (billing is per token produced, not per token allowed).
    with ThreadPoolExecutor(max_workers=min(len(runners), 6)) as executor:
        futures = {}
        for runner in runners:
            futures[executor.submit(
                runner,
                f"REVIEW DOCUMENT TO ANALYZE:\n\n{review_text}",
                prompt,
                24000,
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
              premium     3 models: GPT-6.2 Astra (high) + Gemini 4 Neon + Claude Fable 5.2.
                          The default, and the cheapest panel. Routine cross-checking.
              full        8 models across 7 labs: GPT-6.2 Astra (high) + Qwen4 Max +
                          Sonar Deep Research + GLM 5.3 + Claude Opus 5.5 + Claude Fable 5.2 +
                          Kimi K3.1 Pro + Gemini 4 Neon. Thorough review where breadth of
                          opinion matters. Two members are Anthropic models, so it is less
                          independent of a Claude-based author than the lab count suggests.
              code        5 models across 5 labs: Kimi K3.1 Pro + GPT-6.2 Astra (high) +
                          Qwen4 Max + GLM 5.3 + Claude Fable 5.2. Coding and scientific review.
                          Pass a code-appropriate --system-prompt-file; the built-in adversarial
                          prompt is written for veterinary pharmacology.
              code5       Deprecated alias for `code`, identical roster.
              solo        GPT-6.2 Sol only. The only all-OpenAI tier, so the one to use when you
                          want a reviewer independent of a Claude-based author. One voice, not a
                          panel.

              The former `free` and `mega` tiers were removed in November 2026: the free panel's
              models were delisted upstream, and `mega` was not earning its cost next to `full`.
              There is no zero-cost tier.
        """),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("claim", nargs="?", help="A single claim to verify")
    group.add_argument("--file", "-f", help="File with one claim per line")
    group.add_argument("--review", "-r", help="Review markdown file (auto-extracts claims)")
    group.add_argument("--adversarial", "-a", help="Full-document adversarial review")
    parser.add_argument("--tier", "-t", choices=["premium", "full", "solo", "code", "code5"],
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
