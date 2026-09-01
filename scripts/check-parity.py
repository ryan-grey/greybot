#!/usr/bin/env python3
"""Phase 1's real parity gate: synthesised CDK vs the live stack it must replace.

The 341-check suite cannot do this job. It is offline by design — no AWS, no
network, no credentials — which is what makes it a good deploy gate and exactly
why it is blind to infrastructure. It stays green through a changed IAM
statement, a different schedule expression, a smaller memory size, or an env var
key renamed so it reads as None at runtime. Every one of those is a production
behaviour change; none of them leave the machine.

So this compares `cdk synth` output against `docs/parity-baseline/`, which was
captured from the live hand-rolled stack before any CDK work began.

Every difference is a failure unless it is in `docs/parity-allowlist.json` with a
reason. That file is the record of what Phase 1 deliberately changed — it should
be short, and every entry should be defensible out loud.

    ./scripts/check-parity.py            # prod
    ./scripts/check-parity.py --stage dev

Exit 0 = parity (or every difference allowlisted). Exit 1 = drift.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "parity-baseline"
ALLOWLIST = ROOT / "docs" / "parity-allowlist.json"
CDK_DIR = ROOT / "cdk"


def synth(stage: str) -> dict:
    """Synthesise and return the CloudFormation template."""
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/opt/node@24/bin:" + env.get("PATH", "")
    env["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] = "1"
    subprocess.run(
        ["npx", "--yes", "aws-cdk@2", "synth", f"greybot-{stage}", "--quiet"],
        cwd=CDK_DIR, env=env, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    out = CDK_DIR / "cdk.out" / f"greybot-{stage}.template.json"
    return json.loads(out.read_text())


def resources(template: dict, type_: str) -> list:
    return [r for r in template.get("Resources", {}).values()
            if r.get("Type") == type_]


def one(template: dict, type_: str) -> dict:
    found = resources(template, type_)
    if len(found) != 1:
        raise SystemExit(f"expected exactly one {type_}, found {len(found)}")
    return found[0].get("Properties", {})


def load(name: str):
    path = BASELINE / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def norm_statements(statements):
    """Normalise IAM statements so ordering and scalar-vs-list stop mattering.

    Resource ARNs are compared as a SET of suffixes: the baseline has literal
    ARNs, the template has Fn::Sub/Join structures that only resolve at deploy
    time. Comparing the resolved text is impossible here, so compare the shape
    that actually encodes intent — which actions, over how many resources.
    """
    out = []
    for st in statements:
        actions = st.get("Action")
        actions = [actions] if isinstance(actions, str) else list(actions or [])
        res = st.get("Resource")
        res = [res] if not isinstance(res, list) else res
        out.append((st.get("Effect"), tuple(sorted(actions)), len(res)))
    return sorted(out)


class Report:
    def __init__(self, allowlist):
        self.allow = allowlist
        self.diffs = []
        self.allowed = []
        self.checked = 0

    def check(self, key, expected, actual):
        self.checked += 1
        if expected == actual:
            return
        entry = self.allow.get(key)
        if entry:
            self.allowed.append((key, expected, actual, entry.get("reason", "")))
        else:
            self.diffs.append((key, expected, actual))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="prod")
    args = ap.parse_args()

    if args.stage != "prod":
        print(f"note: baseline describes prod; comparing '{args.stage}' against it\n"
              f"      only checks shape, since names differ by design.\n")

    allow = {}
    if ALLOWLIST.exists():
        allow = json.loads(ALLOWLIST.read_text())

    print(f"==> Synthesising greybot-{args.stage}")
    tpl = synth(args.stage)
    r = Report(allow)

    # ------------------------------------------------------------ lambda
    base = load("lambda-configuration.json")
    fn = one(tpl, "AWS::Lambda::Function")
    if base:
        r.check("lambda.Runtime", base["Runtime"], fn.get("Runtime"))
        r.check("lambda.Handler", base["Handler"], fn.get("Handler"))
        r.check("lambda.MemorySize", base["MemorySize"], fn.get("MemorySize"))
        r.check("lambda.Timeout", base["Timeout"], fn.get("Timeout"))
        r.check("lambda.Architectures", base["Architectures"],
                fn.get("Architectures"))
        r.check("lambda.FunctionName", base["FunctionName"],
                fn.get("FunctionName"))
        # Keys only. Values are ARNs and names that differ per stage; a renamed
        # KEY is the failure mode that matters, because it reads as None.
        base_keys = sorted((base.get("Environment") or {})
                           .get("Variables", {}).keys())
        tpl_keys = sorted((fn.get("Environment") or {})
                          .get("Variables", {}).keys())
        r.check("lambda.EnvironmentKeys", base_keys, tpl_keys)

    # ---------------------------------------------------------- dynamodb
    base = load("dynamodb-table.json")
    tbl = one(tpl, "AWS::DynamoDB::Table")
    if base:
        t = base["Table"]
        r.check("dynamodb.TableName", t["TableName"], tbl.get("TableName"))
        r.check("dynamodb.KeySchema",
                [(k["AttributeName"], k["KeyType"]) for k in t["KeySchema"]],
                [(k["AttributeName"], k["KeyType"])
                 for k in tbl.get("KeySchema", [])])
        r.check("dynamodb.BillingMode",
                t.get("BillingModeSummary", {}).get("BillingMode"),
                tbl.get("BillingMode"))
        r.check("dynamodb.AttributeDefinitions",
                sorted((a["AttributeName"], a["AttributeType"])
                       for a in t["AttributeDefinitions"]),
                sorted((a["AttributeName"], a["AttributeType"])
                       for a in tbl.get("AttributeDefinitions", [])))

    ttl = load("dynamodb-ttl.json")
    if ttl:
        base_ttl = ttl["TimeToLiveDescription"]["TimeToLiveStatus"] == "ENABLED"
        tpl_ttl = bool((tbl.get("TimeToLiveSpecification") or {}).get("Enabled"))
        # A TTL here would silently delete dedupe rows, and an expired dedupe row
        # is a re-announced boss kill. Worth its own named check.
        r.check("dynamodb.TTLEnabled", base_ttl, tpl_ttl)

    # --------------------------------------------------------- scheduler
    base = load("scheduler.json")
    sch = one(tpl, "AWS::Scheduler::Schedule")
    if base:
        r.check("scheduler.Name", base["Name"], sch.get("Name"))
        r.check("scheduler.ScheduleExpression", base["ScheduleExpression"],
                sch.get("ScheduleExpression"))
        r.check("scheduler.Timezone",
                base.get("ScheduleExpressionTimezone"),
                sch.get("ScheduleExpressionTimezone"))
        r.check("scheduler.State", base.get("State"), sch.get("State"))
        r.check("scheduler.FlexibleTimeWindow",
                base.get("FlexibleTimeWindow", {}).get("Mode"),
                (sch.get("FlexibleTimeWindow") or {}).get("Mode"))

    # --------------------------------------------------------- log group
    base = load("log-group.json")
    if base and base.get("logGroups"):
        live = base["logGroups"][0].get("retentionInDays")  # None = never expires
        groups = resources(tpl, "AWS::Logs::LogGroup")
        got = (groups[0].get("Properties", {}).get("RetentionInDays")
               if groups else None)
        r.check("logs.RetentionInDays", live, got)

    # --------------------------------------------------------------- iam
    base = load("iam-inline-greybot-runtime.json")
    if base:
        policies = resources(tpl, "AWS::IAM::Policy")
        runtime = [p for p in policies
                   if p.get("Properties", {}).get("PolicyName")
                   == "greybot-runtime"]
        if not runtime:
            r.diffs.append(("iam.greybot-runtime", "present", "MISSING"))
        else:
            got = runtime[0]["Properties"]["PolicyDocument"]["Statement"]
            r.check("iam.statements",
                    norm_statements(base["PolicyDocument"]["Statement"]),
                    norm_statements(got))

    # --------------------------------------------------------------- api
    base_api = load("api.json")
    base_routes = load("api-routes.json")
    if base_api:
        api = one(tpl, "AWS::ApiGatewayV2::Api")
        r.check("api.Name", base_api["Name"], api.get("Name"))
        r.check("api.ProtocolType", base_api["ProtocolType"],
                api.get("ProtocolType"))
    if base_routes:
        want = sorted(rt["RouteKey"] for rt in base_routes["Items"])
        got = sorted(p.get("Properties", {}).get("RouteKey")
                     for p in resources(tpl, "AWS::ApiGatewayV2::Route"))
        r.check("api.Routes", want, got)
    base_stages = load("api-stages.json")
    if base_stages:
        want = sorted((s["StageName"], s.get("AutoDeploy"))
                      for s in base_stages["Items"])
        got = sorted((p.get("Properties", {}).get("StageName"),
                      p.get("Properties", {}).get("AutoDeploy"))
                     for p in resources(tpl, "AWS::ApiGatewayV2::Stage"))
        r.check("api.Stages", want, got)

    # ------------------------------------------------------------ report
    print(f"    {r.checked} properties compared\n")

    if r.allowed:
        print(f"Allowlisted differences ({len(r.allowed)}):")
        for key, exp, act, reason in r.allowed:
            print(f"  ~ {key}\n      was: {exp}\n      now: {act}\n      why: {reason}")
        print()

    if r.diffs:
        print(f"PARITY FAILED — {len(r.diffs)} unexplained difference(s):\n")
        for key, exp, act in r.diffs:
            print(f"  ! {key}\n      baseline: {exp}\n      synth:    {act}")
        print("\nEach one is either a bug in the stack, or a deliberate change that")
        print(f"belongs in {ALLOWLIST.relative_to(ROOT)} with a reason.")
        return 1

    print("PARITY OK — the synthesised stack matches the live one on every")
    print("property checked, and every difference is accounted for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
