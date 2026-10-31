"""Check the Claude plan token from inside the factory.

  python -m factory.claude_token check     show which token is loaded, its expiry, and make one tiny call

The token itself is obtained on the Mac with `make claude-token` (scripts/claude-token.sh), which runs
`claude setup-token` with your current claude.ai login. This module never prints the token.
"""
from __future__ import annotations

import argparse
import sys
import time

from factory.config import CLAUDE_TOKEN_LIFETIME_DAYS, claude_oauth_token, claude_token_warning


def mask(token: str) -> str:
    return f"{token[:13]}…{token[-4:]}" if len(token) > 20 else "…"


def check() -> int:
    token, created = claude_oauth_token()
    if not token:
        print("No Claude plan token found. On the Mac, run: make claude-token")
        return 1
    print(f"Token:   {mask(token)}")
    if created:
        left = CLAUDE_TOKEN_LIFETIME_DAYS - int((time.time() - created) // 86400)
        print(f"Expires: in about {left} days")
    else:
        print("Expires: unknown (stored without a creation date)")
    warning = claude_token_warning()
    if warning:
        print(f"Warning: {warning}")

    from factory.pool import QuotaExhausted, SubscriptionAdapter
    try:
        r = SubscriptionAdapter().chat.completions.create(
            model=None, max_tokens=8, messages=[{"role": "user", "content": "Reply with exactly: ok"}])
    except QuotaExhausted as e:
        print(f"Token works, but the Claude plan usage limit is reached right now: {e}")
        return 2
    except SystemExit as e:
        print(f"Not usable: {e}")
        return 1
    except Exception as e:                       # message from Claude Code; it never contains the token
        print(f"Claude rejected the call: {e}")
        print("If the token was revoked or expired, run on the Mac: make claude-token")
        return 1
    print(f"OK: Claude answered ({r.choices[0].message.content.strip()[:20]!r}) using your plan token.")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="claude_token")
    ap.add_argument("cmd", choices=["check"])
    ap.parse_args(argv)
    sys.exit(check())


if __name__ == "__main__":
    main()
