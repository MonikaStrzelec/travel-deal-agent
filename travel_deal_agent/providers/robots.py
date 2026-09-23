"""Shared robots.txt policy for HTTP-only providers (ITAKA, TUI, Wakacje.pl).

Extracted from three byte-identical implementations; behavior is unchanged:
conservative union of all Disallow rules (even those for other user agents),
fail-closed on missing/malformed robots.txt, and Crawl-delay honored as a
lower bound on request spacing.
"""

import re


def robots_policy(text: str, path: str) -> float:
    """Conservative union of all disallows, even those for other user agents.

    Allow directives never override a disallow. Missing/HTML robots fail closed.
    """
    if "user-agent:" not in text.lower() or "<html" in text.lower():
        raise ValueError("Unrecognized robots.txt")
    delay = 0.0
    for line in text.splitlines():
        key, separator, value = line.split("#", 1)[0].partition(":")
        if not separator:
            continue
        value = value.strip()
        if key.strip().lower() == "disallow" and value:
            terminal = value.endswith("$")
            pattern = re.escape(value[:-1] if terminal else value).replace(r"\*", ".*")
            if re.match(pattern + ("$" if terminal else ""), path):
                raise ValueError("Listing forbidden by robots.txt")
        if key.strip().lower() == "crawl-delay":
            parsed = float(value)
            if not 0 <= parsed <= 86400:
                raise ValueError("Invalid crawl delay")
            delay = max(delay, parsed)
    return delay
