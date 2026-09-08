"""Forward signed role-button requests to the NAS without logging their contents."""
import json
import urllib.error
import urllib.request


def forward(raw, signature, timestamp):
    request = urllib.request.Request("https://greybot.ryangrey.dev/discord/roles", data=raw,
        headers={"Content-Type": "application/json",
                 "User-Agent": "greyBot/1.0 (+https://greybot.ryangrey.dev/about)",
                 "X-Signature-Ed25519": signature,
                 "X-Signature-Timestamp": timestamp}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=1.7) as response:
            body = json.loads(response.read(16384))
            if body.get("type") not in {4, 9}:
                raise ValueError("Unexpected role response")
            return body
    except Exception as exc:
        # Do not log the request, body, headers, exception text or interaction token.
        diagnostic = {"event": "role_relay_failed", "error_type": type(exc).__name__}
        if isinstance(exc, urllib.error.HTTPError):
            diagnostic["http_status"] = exc.code
        print(json.dumps(diagnostic))
        return {"type": 4, "data": {"content": "The role service did not confirm your request. Check your roles before trying again.",
                                    "flags": 64, "allowed_mentions": {"parse": []}}}
