"""Render account-scoped IAM policy to a private local file before applying it.

Usage: python3 infra/render-cdk-exec-policy.py /private/path/policy.json
Uses the active AWS profile; never apply the source template directly.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

def render(template, account):
    if len(account) != 12 or not account.isdigit():
        raise ValueError("AWS account lookup failed")
    return json.loads(template.replace("${AWS_ACCOUNT_ID}", account))

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Supply a private output path outside the repository")
    output = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if output.is_relative_to(root):
        raise SystemExit("Rendered policy must remain outside the repository")
    account = subprocess.check_output(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        text=True).strip()
    policy = render((root / "infra/cdk-exec-supplement-policy.json").read_text(), account)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(policy, stream, indent=2)
        stream.write("\n")
    print("Private policy rendered; source permissions are unchanged.")
