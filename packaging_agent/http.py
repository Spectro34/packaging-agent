"""
HTTP helpers and GPT wrapper for the openSUSE Packaging Agent.
"""

import json
import time
import urllib.request
import urllib.error

from packaging_agent.config import SSL_CTX, OPENAI_API


def http_get(url, headers=None, timeout=20, auth=None):
    """GET request. Returns response text."""
    import base64
    hdrs = {"User-Agent": "obs-maintenance-agent/2.0"}
    if headers:
        hdrs.update(headers)
    if auth:
        cred = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        hdrs["Authorization"] = f"Basic {cred}"
    req = urllib.request.Request(url, headers=hdrs)
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, headers=None, timeout=20):
    """GET request, parse JSON response."""
    data = http_get(url, headers, timeout)
    return json.loads(data)


def http_post_json(url, body, headers=None, timeout=30):
    """POST JSON body, parse JSON response."""
    hdrs = {"Content-Type": "application/json", "User-Agent": "obs-maintenance-agent/2.0"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    return json.loads(resp.read())


def _is_low_quality_response(text):
    """Detect generic/vague LLM responses that should be retried."""
    low_quality_phrases = [
        "i cannot determine", "i don't have enough", "unable to analyze",
        "more information is needed", "without access to", "i would need to see",
        "it's difficult to say", "hard to determine without",
    ]
    text_lower = text.lower()
    if any(phrase in text_lower for phrase in low_quality_phrases):
        return True
    if len(text.strip()) < 50:
        return True
    return False


def strip_markdown(text):
    """Strip markdown code block wrappers from GPT output."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        elif lines[0].strip().startswith("```"):
            lines = lines[1:]
        t = "\n".join(lines)
    return t


def gpt(system_prompt, user_prompt, api_key, temperature=0.2, max_tokens=1500,
        json_mode=False, retries=4, model=None):
    """Call GPT with quality-check retry loop.

    Args:
        system_prompt: System message for the AI
        user_prompt: User message content
        api_key: OpenAI API key
        temperature: Sampling temperature (0.0-1.0)
        max_tokens: Max response tokens
        json_mode: If True, request JSON response format
        retries: Number of retry attempts for low-quality responses
        model: Override model name (default: from config or gpt-4o)
    """
    if not api_key:
        return "[GPT skipped — no API key]"

    model = model or "gpt-4o"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    max_attempts = retries + 1
    for attempt in range(max_attempts):
        try:
            resp = http_post_json(OPENAI_API, body,
                                  headers={"Authorization": f"Bearer {api_key}"}, timeout=90)
            text = resp["choices"][0]["message"]["content"]
            if attempt < retries and _is_low_quality_response(text):
                continue
            return text
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                wait = min(2 ** attempt * 10, 120)  # 10s, 20s, 40s, 80s, 120s
                print(f"         [GPT] Rate limited, waiting {wait}s (attempt {attempt + 1}/{max_attempts})...")
                time.sleep(wait)
                continue
            if attempt < retries:
                continue
            return f"[GPT error: {e}]"
        except Exception as e:
            if attempt < retries:
                continue
            return f"[GPT error: {e}]"
    return "[GPT error: all retries failed]"
