"""The greyBot stack.

Phase 1: the CURRENT single-tenant bot, expressed in CDK, with deploy parity and
zero behaviour change. Multi-tenancy lands on top of this, not inside it.

Everything here is transcribed from `docs/parity-baseline/`. Where CDK's default
differs from what the hand-rolled deploy built, the hand-rolled value wins and
carries a comment saying so — the point of this phase is that nothing moved.
"""

from pathlib import Path

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_scheduler as scheduler,
)
from constructs import Construct

from .config import StageConfig

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "build" / "lambda"


def _assert_package() -> str:
    """The build/lambda directory, or a synth that stops rather than a bot that 401s.

    Resolved from this file rather than from the process's working directory, so it means
    the same thing whether cdk is run from cdk/ or from the repo root.

    The `nacl` check is the one that matters. Every other way this package can be wrong is
    loud -- a missing handler.py fails the first invocation -- but a missing PyNaCl fails
    only inside `interactions.verify`, which is required to fail CLOSED, so the bot keeps
    polling and announcing perfectly while every slash command answers 401. Synth is the
    last place that can still notice.
    """
    if not (PACKAGE_DIR / "nacl").is_dir():
        raise SystemExit(
            f"{PACKAGE_DIR} is missing or has no vendored PyNaCl.\n"
            "Run scripts/build-lambda.sh before cdk deploy — deploying src/ directly "
            "ships a function that answers every Discord slash command with a 401.")
    return str(PACKAGE_DIR)


class GreybotStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 cfg: StageConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        # Prod retains its stateful resources; dev destroys them.
        #
        # RETAIN is what protects the prod dedupe rows and the prod API id. On dev
        # it is actively harmful: a rolled-back deploy leaves the table and log
        # group behind, and the NEXT deploy then fails with "already exists" — so
        # one failure makes the stage permanently undeployable until somebody
        # deletes the orphans by hand. Hit exactly that twice while bringing dev
        # up. Dev holds nothing worth keeping, so it should clean up after itself.
        self.removal = (RemovalPolicy.RETAIN if cfg.is_prod
                        else RemovalPolicy.DESTROY)

        self.table = self._table()
        self.role = self._role()
        self.function = self._function()
        self.api = self._api()
        self._schedule()

        CfnOutput(self, "FunctionName", value=cfg.function_name)
        CfnOutput(self, "TableName", value=cfg.table_name)
        CfnOutput(self, "InteractionsUrl",
                  value=f"https://{self.api.ref}.execute-api.{Aws.REGION}.amazonaws.com/interactions")

    # ---------------------------------------------------------------- table

    def _table(self) -> dynamodb.Table:
        """The dedupe/state table.

        In prod this is ADOPTED by `cdk import`, not created. It already holds
        the rows that make posting exactly-once: each announced kill is recorded
        by a conditional write, and the write IS the permission to post. Lose a
        row and the bot re-announces a boss kill into a live channel.

        Hence RETAIN. Even with the import done correctly, a future `cdk destroy`
        or a logical-ID change must never be able to take the table with it.
        The item count at import time is in the parity baseline; it must match
        after.
        """
        return dynamodb.Table(
            self, "StateTable",
            table_name=self.cfg.table_name,
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING),
            # PAY_PER_REQUEST in the baseline. Also the right shape for a poller
            # that does a handful of reads every 15 minutes.
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # TTL is DISABLED in the baseline. Do not enable it here to be tidy:
            # a TTL on this table would silently delete dedupe rows, and an
            # expired dedupe row is a re-announced kill.
            removal_policy=self.removal,
            # Off in the baseline. Worth revisiting once the table is
            # multi-tenant and holds other people's config, but turning it on
            # here would be a change, and this phase changes nothing.
            point_in_time_recovery_specification=
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=False),
        )

    # ----------------------------------------------------------------- iam

    def _role(self) -> iam.Role:
        """The runtime role, statement-for-statement from the baseline.

        Written out longhand rather than using CDK's `grant_*` helpers. The
        helpers are better practice in general, and wrong here: they generate
        their own action sets (`grant_read_write_data` adds BatchGet, Query,
        Scan, DeleteItem) and Phase 1's claim is that permissions did not widen.
        Deliberate exact transcription beats a convenient superset.
        """
        cfg = self.cfg
        role = iam.Role(
            self, "RuntimeRole",
            role_name=cfg.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        policy = iam.Policy(self, "RuntimePolicy", policy_name="greybot-runtime")

        policy.document.add_statements(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream",
                         "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                           f"log-group:/aws/lambda/{cfg.function_name}:*"],
            ),
            # GetItem, PutItem, UpdateItem. No Query, no Scan, no DeleteItem --
            # same discipline as GreyScale, and the absence is the security
            # property. Do not add actions here to make a future feature easier;
            # add them in the phase that needs them, with the reason.
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:PutItem",
                         "dynamodb:UpdateItem"],
                resources=[f"arn:aws:dynamodb:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                           f"table/{cfg.table_name}"],
            ),
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                    f"parameter{cfg.ssm_prefix}/{leaf}"
                    for leaf in cfg.ssm_leaves
                ],
            ),
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[f"arn:aws:kms:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                           f"key/{cfg.kms_key_id}"],
            ),
            # Self-invoke: the interactions handler hands slow work to a second
            # asynchronous invocation of itself, because Discord requires a
            # response within 3 seconds.
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                           f"function:{cfg.function_name}"],
            ),
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[f"arn:aws:sns:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                           f"{cfg.alerts_topic_name}"],
            ),
        )
        policy.attach_to_role(role)
        # PutObject ONLY, and only under the one bucket, and only when a bucket is
        # configured for this stage. Not ListBucket, not GetObject, not Delete: the bot
        # writes a night's page and never needs to read one back or remove one. Dev
        # configures no bucket and therefore gets no statement at all -- a dev deploy
        # must not be able to overwrite a published night.
        if cfg.recap_page_bucket:
            role.add_to_policy(iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"arn:aws:s3:::{cfg.recap_page_bucket}/*"],
            ))

        return role

    # ------------------------------------------------------------- function

    def _function(self) -> lambda_.Function:
        cfg = self.cfg

        # Explicit, so the retention is ours rather than "never expires by
        # default". The hand-rolled stack left the default; this is the one
        # deliberate difference in Phase 1 and it is recorded in the allowlist.
        logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/aws/lambda/{cfg.function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=self.removal,
        )

        return lambda_.Function(
            self, "Function",
            function_name=cfg.function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=cfg.handler,
            # build/lambda, NOT src/. src/ is the source; the package is the source plus
            # the vendored linux/aarch64 PyNaCl that Ed25519 verification needs and the
            # runtime does not carry. Shipping src/ directly is what silently 401'd every
            # slash command from the CDK cutover onward -- see scripts/build-lambda.sh,
            # which is what produces this directory and refuses to produce an incomplete
            # one. Run it before `cdk deploy`; _assert_package below is the backstop.
            code=lambda_.Code.from_asset(_assert_package()),
            memory_size=cfg.memory_mb,
            timeout=Duration.seconds(cfg.timeout_seconds),
            role=self.role,
            environment={
                # Keys matter: a renamed key reads as None at runtime rather than
                # failing, so the bot would run and quietly do the wrong thing.
                "STATE_TABLE": cfg.table_name,
                "ANNOUNCE_TZ": cfg.announce_tz,
                "REPO_URL": cfg.repo_url,
                # SSM_PREFIX is set on DEV ONLY, and its absence on prod is the
                # point. `src/config.py` already defaults to "/greybot", so prod
                # reads the same paths it always has and its env var KEYS stay
                # byte-identical to the parity baseline — adding a fourth key
                # there would fail the parity check for no behavioural gain.
                #
                # Dev needs it: without it the dev function reads prod's
                # parameter paths. Its dev-scoped role denies that, which is the
                # isolation working, but the result is a dev bot that cannot
                # start rather than one pointed at the right tree.
                **({} if cfg.is_prod else {"SSM_PREFIX": cfg.ssm_prefix}),
            },
        )

    # ------------------------------------------------------------------ api

    def _api(self) -> apigw.CfnApi:
        """HTTP API for Discord interactions.

        L1 constructs on purpose. The L2 `HttpApi` adds a CORS block and its own
        default stage naming; this has to land on `$default` with auto-deploy and
        exactly one route, matching the baseline.
        """
        cfg = self.cfg

        api = apigw.CfnApi(
            self, "InteractionsApi",
            name=cfg.api_name,
            protocol_type="HTTP",
        )
        # RETAIN, because this API's id is baked into a URL registered by hand in
        # the Discord developer portal. Deleting the stack would mint a new id on
        # the next deploy, and the interactions endpoint would go dead until
        # someone remembered to paste the new URL into Discord — a failure with no
        # error message anywhere in AWS. The table is retained for its data; this
        # is retained for its name.
        api.apply_removal_policy(self.removal)

        integration = apigw.CfnIntegration(
            self, "InteractionsIntegration",
            api_id=api.ref,
            integration_type="AWS_PROXY",
            integration_uri=self.function.function_arn,
            payload_format_version="2.0",
        )

        apigw.CfnRoute(
            self, "InteractionsRoute",
            api_id=api.ref,
            route_key="POST /interactions",
            target=f"integrations/{integration.ref}",
        )

        apigw.CfnStage(
            self, "DefaultStage",
            api_id=api.ref,
            stage_name="$default",
            auto_deploy=True,
        )

        # Scoped to this API's execute-api ARN, not a bare service principal --
        # otherwise any API Gateway in the account could invoke the function.
        self.function.add_permission(
            "ApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=f"arn:aws:execute-api:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                       f"{api.ref}/*/*/interactions",
        )
        return api

    # ------------------------------------------------------------- schedule

    def _schedule(self) -> None:
        """The poller.

        rate(15 minutes), UTC, FlexibleTimeWindow OFF. The cadence is
        load-bearing: it sets the WCL points spend, which is the constraint the
        whole multi-tenant phase is designed around. Changing it here changes the
        budget, so it lives in config with the rest of the parity values.
        """
        cfg = self.cfg

        scheduler_role = iam.Role(
            self, "SchedulerRole",
            role_name=cfg.scheduler_role_name,
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[self.function.function_arn],
        ))

        scheduler.CfnSchedule(
            self, "PollSchedule",
            name=cfg.schedule_name,
            schedule_expression=cfg.schedule_expression,
            schedule_expression_timezone=cfg.schedule_timezone,
            state="ENABLED",
            # OFF, not the 15-minute window CDK would otherwise leave you with.
            # A flexible window means the poll drifts, and the announcer's
            # freshness claim is a function of when it actually ran.
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.function.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
        )
