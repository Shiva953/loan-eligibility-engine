"""Save the emails Workflow C rendered in its latest run as HTML files (`make email-preview`).

Why: in suppressed mode (the default until the owner sets up the SES-only sender) Workflow C
renders the first EMAIL_CAP emails as previews and sends nothing. This takes them out of the stored
n8n execution, so the exact HTML an inbox would receive can be opened in a browser, and prints
each subject, recipient and plain-text body.

Usage:
    uv run python scripts/email_preview.py
"""

import sys

from scripts.envfile import REPO_ROOT
from scripts.n8n_execution import fetch_execution
from scripts.n8n_ids import WORKFLOWS

OUT_DIR = REPO_ROOT / "build" / "email-preview"


def main() -> int:
    """Save each rendered email of Workflow C's latest execution as an HTML file."""
    exec_id, data = fetch_execution(WORKFLOWS["c_notification"], None)
    runs = data["resultData"]["runData"].get("Render emails")
    if not runs:
        print(f"execution {exec_id}: Workflow C rendered nothing (did it fail earlier?)")
        return 1
    emails = [item["json"] for item in runs[0]["data"]["main"][0]]
    emails = [e for e in emails if not e.get("none")]
    if not emails:
        print(f"execution {exec_id}: nobody to email in that batch")
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.html"):
        old.unlink()
    for index, email in enumerate(emails, start=1):
        path = OUT_DIR / f"email-{index}.html"
        path.write_text(email["html"], encoding="utf-8")
        state = f"would send to {email['to']}" if email["send"] else "preview only, not sent"
        print(f"--- {path.relative_to(REPO_ROOT)}  ({state})")
        print(f"subject: {email['subject']}\n")
        print(email["text"])
        print()
    print(
        f"execution {exec_id}: {len(emails)} email(s) written to {OUT_DIR.relative_to(REPO_ROOT)}/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
