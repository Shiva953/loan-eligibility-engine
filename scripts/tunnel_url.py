"""Print the current cloudflared quick-tunnel URL.

Why this is needed: a quick tunnel gets a new random ``*.trycloudflare.com`` hostname every time
it starts. The notify Lambda reads the URL from SSM at runtime, so it has to be re-read after
every restart. Writing it to SSM waits until the AWS account is usable (docs/DECISIONS.md B1).

The cloudflared metrics server answers ``/quicktunnel`` with the hostname. If that fails, the
URL is taken from the container logs.

Usage:
    uv run python scripts/tunnel_url.py
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from scripts.envfile import load_settings

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def from_metrics(port: str) -> str | None:
    """Ask cloudflared's metrics server for the quick-tunnel hostname."""
    try:
        url = f"http://127.0.0.1:{port}/quicktunnel"
        with urllib.request.urlopen(url, timeout=5) as response:
            hostname = json.loads(response.read()).get("hostname", "")
    except urllib.error.URLError, json.JSONDecodeError, TimeoutError:
        return None
    return f"https://{hostname}" if hostname else None


def from_logs() -> str | None:
    """Fall back to the last trycloudflare URL printed in the container logs."""
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", "cloudflared"],
        capture_output=True,
        text=True,
        check=False,
    )
    matches = URL_RE.findall(result.stdout + result.stderr)
    return matches[-1] if matches else None


def main() -> int:
    """Print the URL, or explain how to start the tunnel."""
    settings = load_settings()
    url = from_metrics(settings.get("CLOUDFLARED_METRICS_HOST_PORT", "20241")) or from_logs()
    if not url:
        print("no tunnel URL found: start it with `make tunnel`", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
