#!/usr/bin/env python3
"""Connect the operator's Warcraft Logs account without printing or saving credentials locally.

Register http://127.0.0.1:8769/callback on the existing WCL client first.
Run with AWS_PROFILE=infra; open the printed local start URL in your browser.
--refresh renews the encrypted grant without a browser when the provider permits it.
"""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

ORIGIN = "https://www.warcraftlogs.com"
REDIRECT = "http://127.0.0.1:8769/callback"


def request(url, data, headers):
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except (urllib.error.URLError, ValueError):
        raise RuntimeError("Warcraft Logs request failed; credential details withheld") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="/greybot")
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--verify-report", required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    ssm = boto3.client("ssm", region_name="us-east-1")
    prefix = args.prefix.rstrip("/") + "/wcl/"
    params = ssm.get_parameters(Names=[prefix + "client_id", prefix + "client_secret"],
                                WithDecryption=True)
    values = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in params["Parameters"]}
    client_id, client_secret = values["client_id"], values["client_secret"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    def save(form, previous=None):
        payload = request(ORIGIN + "/oauth/token", urllib.parse.urlencode(form).encode(),
                          {"Authorization": "Basic " + basic,
                           "Content-Type": "application/x-www-form-urlencoded"})
        token = payload.get("access_token")
        if not token or float(payload.get("expires_in", 0)) <= 0:
            raise RuntimeError("No valid expiring access token returned")
        query = {"query": "query($code:String!){userData{currentUser{id name}} "
                 "reportData{report(code:$code){code visibility guild{id}}}}",
                 "variables": {"code": args.verify_report}}
        check = request(ORIGIN + "/api/v2/user", json.dumps(query).encode(),
                        {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        if check.get("errors"):
            raise RuntimeError("The authorized account cannot read the verification report")
        data = check.get("data") or {}
        user = (data.get("userData") or {}).get("currentUser") or {}
        report = (data.get("reportData") or {}).get("report") or {}
        if user.get("name", "").casefold() != args.expected_user.casefold():
            raise RuntimeError("Authorized account does not match the requested account")
        if report.get("code") != args.verify_report:
            raise RuntimeError("Verification report unavailable")
        bundle = {"access_token": token,
                  "refresh_token": payload.get("refresh_token") or (previous or {}).get("refresh_token", ""),
                  "expires_at": time.time() + float(payload["expires_in"]),
                  "client_id": client_id, "user_id": user["id"], "user_name": user["name"]}
        key = ssm.describe_parameters(ParameterFilters=[
            {"Key": "Name", "Option": "Equals", "Values": [prefix + "client_secret"]}])["Parameters"][0]["KeyId"]
        result = ssm.put_parameter(Name=prefix + "user_auth", Type="SecureString", KeyId=key,
                                   Value=json.dumps(bundle), Overwrite=True,
                                   Description="Operator-authorized Warcraft Logs account grant")
        print(json.dumps({"connected_user": user["name"], "report_verified": report["code"],
                          "visibility": report.get("visibility"), "expires_at": bundle["expires_at"],
                          "refresh_available": bool(bundle["refresh_token"]),
                          "ssm_version": result["Version"]}), flush=True)

    if args.refresh:
        previous = json.loads(ssm.get_parameter(Name=prefix + "user_auth", WithDecryption=True)["Parameter"]["Value"])
        if previous.get("client_id") != client_id or not previous.get("refresh_token"):
            raise RuntimeError("Reconnect this account in the browser")
        save({"grant_type": "refresh_token", "refresh_token": previous["refresh_token"]}, previous)
        return

    outcome = {"done": False, "ok": False}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass  # The callback URL contains a one-time credential.

        def do_GET(self):
            if self.headers.get("Host") != "127.0.0.1:8769":
                self.send_error(400)
                return
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/start":
                target = ORIGIN + "/oauth/authorize?" + urllib.parse.urlencode({
                    "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
                    "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            query = urllib.parse.parse_qs(url.query)
            if url.path != "/callback" or not secrets.compare_digest(query.get("state", [""])[0], state):
                self.send_error(400)
                return
            outcome["done"] = True
            try:
                code = query.get("code", [""])[0]
                if not code or query.get("error"):
                    raise RuntimeError("Account authorization was not granted")
                save({"grant_type": "authorization_code", "redirect_uri": REDIRECT,
                      "code": code, "code_verifier": verifier})
                outcome["ok"] = True
                message = "Warcraft Logs connected. You can close this tab."
            except Exception:
                message = "Connection failed. No credentials were displayed. Check the operator console."
                print("Account connection failed; credential details withheld", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(("<script>history.replaceState(null,'','/done')</script><p>" + message + "</p>").encode())

    with HTTPServer(("127.0.0.1", 8769), Callback) as server:
        server.timeout = 1
        deadline = time.monotonic() + 900
        print("Open http://127.0.0.1:8769/start in Chrome to connect Warcraft Logs.", flush=True)
        while not outcome["done"] and time.monotonic() < deadline:
            server.handle_request()
    if not outcome["ok"]:
        sys.exit("Account was not connected")


if __name__ == "__main__":
    main()
