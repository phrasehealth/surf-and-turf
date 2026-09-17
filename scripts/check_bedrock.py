#!/usr/bin/env python
"""Connectivity check for the Bedrock model access the agent needs.

    python scripts/check_bedrock.py

Two stages, because they fail for different reasons and it is useful to know
which one broke:

  1. boto3 Converse  -- credentials, region and model access, direct to the
                        bedrock-runtime API. No Claude Code involved.
  2. Agent SDK       -- the real path: ClaudeAgentOptions -> the Claude Code
                        binary as a subprocess, talking to Bedrock.

Stage 2 spends a fraction of a cent. Skip it with --no-sdk. No secret is
printed; the bearer token is reported only by length.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from check_snowflake import OK, BAD, WARN, load_env_file  # noqa: E402

PROMPT = "Reply with the single word: OK"

HINTS = [
    ("accessdenied", "model access is not enabled for this account/region: Bedrock console -> "
                     "Model access -> enable the Anthropic models, or the API key's policy "
                     "is missing bedrock:InvokeModel"),
    ("unrecognizedclient", "the bearer token is not valid for this region -- Bedrock API keys "
                           "are issued per region; check AWS_REGION"),
    ("expiredtoken", "the bearer token has expired; generate a new one in the Bedrock console"),
    ("validationexception", "ANTHROPIC_MODEL is not a usable id here. Bedrock ids are exact: a "
                            "region prefix (us./eu./global.) and, for dated models, the full "
                            "version suffix -- see the profiles listed below"),
    ("resourcenotfound", "the inference profile does not exist in AWS_REGION"),
    ("could not connect", "no network route to bedrock-runtime in this region"),
]


def hint_for(message: str) -> str | None:
    lowered = message.lower().replace("_", "").replace(" ", "")
    for needle, hint in HINTS:
        if needle.replace(" ", "") in lowered:
            return hint
    return None


def preflight(settings) -> bool:
    print("config")
    print(f"  {OK if settings.use_bedrock else WARN} use_bedrock  "
          f"{settings.use_bedrock}  (CLAUDE_CODE_USE_BEDROCK)")
    print(f"  {OK if settings.aws_region else BAD} region       {settings.aws_region or '<empty>'}")
    print(f"  {OK if settings.model else WARN} model        "
          f"{settings.model or '<unset, the CLI default applies>'}")
    print(f"  {OK} agent_mode   {settings.agent_mode}")

    print("auth")
    token = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    profile, key_id = os.getenv("AWS_PROFILE", ""), os.getenv("AWS_ACCESS_KEY_ID", "")
    if token:
        print(f"  {OK} bearer token  <set, {len(token)} chars>")
        if profile or key_id:
            print(f"  {WARN} AWS_PROFILE/AWS_ACCESS_KEY_ID are also set; the bearer token wins "
                  f"for Bedrock calls")
    elif profile or key_id:
        print(f"  {OK} IAM chain     profile={profile or '<default>'}")
    else:
        print(f"  {BAD} no AWS_BEARER_TOKEN_BEDROCK, AWS_PROFILE or AWS_ACCESS_KEY_ID")
        return False

    if not settings.use_bedrock:
        print(f"  {WARN} CLAUDE_CODE_USE_BEDROCK is off, so the agent would call the Anthropic "
              f"API, not Bedrock")
    return bool(settings.aws_region)


def converse(settings) -> bool:
    """Stage 1: hit bedrock-runtime directly."""
    import boto3

    print("boto3 -> bedrock-runtime")
    model = settings.model or "us.anthropic.claude-sonnet-4-5"
    try:
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": PROMPT}]}],
            inferenceConfig={"maxTokens": 16},  # no temperature: deprecated on Claude 5
        )
    except Exception as exc:
        message = str(exc)
        print(f"  {BAD} converse failed\n      {message.strip().splitlines()[0]}")
        hint = hint_for(f"{type(exc).__name__} {message}")
        if hint:
            print(f"      hint: {hint}")
        if "validation" in f"{type(exc).__name__}{message}".lower():
            list_profiles(settings)
        return False

    text = "".join(b.get("text", "") for b in response["output"]["message"]["content"]).strip()
    usage = response.get("usage", {})
    print(f"  {OK} {model}")
    print(f"      replied {text!r}  "
          f"(in {usage.get('inputTokens')} / out {usage.get('outputTokens')} tokens)")
    return True


def list_profiles(settings) -> None:
    """Print the Anthropic inference profiles this account can call."""
    import boto3

    try:
        summaries = boto3.client("bedrock", region_name=settings.aws_region).list_inference_profiles(
            maxResults=200
        )["inferenceProfileSummaries"]
    except Exception as exc:
        print(f"      (could not list inference profiles: {type(exc).__name__})")
        return
    ids = sorted(
        p["inferenceProfileId"] for p in summaries
        if "anthropic" in p["inferenceProfileId"] and p.get("status") == "ACTIVE"
    )
    if not ids:
        print("      (no Anthropic inference profiles are active in this region)")
        return
    print(f"      available in {settings.aws_region}, set one as ANTHROPIC_MODEL:")
    for profile_id in ids:
        print(f"        {profile_id}")


async def sdk_roundtrip(settings) -> bool:
    """Stage 2: the path app/agent.py actually uses."""
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )

    print("claude agent sdk -> claude code -> bedrock")
    env: dict[str, str] = {}
    if settings.use_bedrock:
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env["AWS_REGION"] = settings.aws_region

    options = ClaudeAgentOptions(
        model=settings.model,
        env=env,                      # merged into the inherited environment by the SDK
        max_turns=1,
        allowed_tools=[],             # no tools: this is a liveness check, not a task
        permission_mode="dontAsk",
        system_prompt="Answer in one word.",
        stderr=lambda line: None,
    )

    text, result = "", None
    try:
        async for msg in query(prompt=PROMPT, options=options):
            if isinstance(msg, AssistantMessage):
                text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                result = msg
    except Exception as exc:
        message = str(exc)
        print(f"  {BAD} sdk call failed\n      {message.strip().splitlines()[0]}")
        hint = hint_for(message)
        if hint:
            print(f"      hint: {hint}")
        elif "not found" in message.lower() or "enoent" in message.lower():
            print(f"      hint: the Claude Code binary is missing; the claude-agent-sdk package "
                  f"bundles it, so reinstall requirements.txt in this venv")
        return False

    if result is not None and result.is_error:
        print(f"  {BAD} turn ended with an error (subtype={result.subtype})")
        return False
    print(f"  {OK} replied {text.strip()[:60]!r}")
    if result is not None:
        cost = f"${result.total_cost_usd:.6f}" if result.total_cost_usd else "n/a"
        print(f"      cost {cost}, {result.duration_ms} ms, {result.num_turns} turn(s)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--no-sdk", action="store_true",
                        help="stop after the direct boto3 call (spends nothing on a turn)")
    parser.add_argument("--no-boto", action="store_true",
                        help="skip the direct call and only exercise the SDK path")
    args = parser.parse_args()

    if not args.no_env_file:
        n = load_env_file(Path(args.env_file))
        print(f"loaded {n} variable(s) from {args.env_file}\n")

    from app.config import settings

    if not preflight(settings):
        print(f"\n{BAD} fix the above before calling Bedrock")
        return 1
    print()

    ok = True
    if not args.no_boto:
        ok = converse(settings)
        print()
        if not ok:
            print(f"{BAD} the direct call failed, so the SDK path cannot work either")
            return 1
    if not args.no_sdk:
        ok = asyncio.run(sdk_roundtrip(settings))
        print()

    if ok:
        print(f"{OK} Bedrock is reachable: the agent can call {settings.model}")
        if settings.agent_mode != "sdk":
            print(f"  set AGENT_MODE=sdk in .env to use it (currently {settings.agent_mode!r})")
        return 0
    print(f"{BAD} not usable yet -- see the failure above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
