#!/usr/bin/env python3
"""greyBot CDK app.

Two stages, deployed one at a time and by hand:

    cdk deploy greybot-dev      # throwaway Discord app + test server
    cdk deploy greybot-prod     # the live scrambled bot

Phase 1 keeps deployment local rather than moving it into CI at the same time.
The phase's whole claim is that the deploy produced an identical stack; changing
the deploy mechanism in the same step would make a failure ambiguous.
"""
import os

import aws_cdk as cdk

from greybot.config import STAGES
from greybot.stack import GreybotStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT") or "164106395035",
    region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
)

for name, cfg in STAGES.items():
    GreybotStack(app, f"greybot-{name}", cfg=cfg, env=env,
                 description=f"greyBot ({name}) — Phase 1 CDK port")

app.synth()
