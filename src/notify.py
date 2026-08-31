"""Email, by way of the alert pipeline that already exists.

greyBot does not talk to SES. It publishes to `ryangrey-dev-alerts`, the SNS topic that
already fans out to ryangrey-alert-forwarder, which formats the message and sends it from
alerts@ryangrey.dev over a DKIM-signed domain identity.

That indirection is worth one extra hop for a reason learned the hard way on the website:
SNS's own email sender never delivered to the target Gmail address -- four subscription
attempts, zero arrivals -- while mail from an authenticated domain we own gets through.
Wiring a second SES sender into this Lambda would mean a second sender reputation, a
second set of DNS records and a second thing to debug the next time mail goes missing.

The forwarder falls through to `body` verbatim for anything that is not a CloudWatch
alarm, so a plain string with an SNS Subject arrives as a readable email with no changes
on its side at all.

A failed publish RAISES rather than returning quietly, and handler.py catches it around
the whole health check -- so the poll survives, and the reason the mail did not go lands in
the log instead of being swallowed here where nothing can write it down.
"""

import os

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")

# SNS caps Subject at 100 characters and rejects newlines outright, which would fail the
# publish rather than truncate it.
SUBJECT_MAX = 100

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10)
sns = boto3.client("sns", region_name=REGION, config=_cfg)


class NotifyError(RuntimeError):
    pass


def clean_subject(text):
    """One line, ASCII, inside the cap. Non-ASCII is dropped rather than encoded: SNS
    rejects the publish outright, and an em dash is not worth losing an alert over."""
    one_line = " ".join(str(text or "greyBot alert").split())
    one_line = one_line.encode("ascii", "ignore").decode("ascii") or "greyBot alert"
    return one_line[:SUBJECT_MAX - 3] + "..." if len(one_line) > SUBJECT_MAX else one_line


def publish(topic_arn, subject, body):
    """Send one alert.

    A missing topic ARN is a configuration choice, not a failure -- an unset
    /greybot/alerts/sns_topic_arn is how these are turned off -- so it returns False
    rather than raising. Anything else that goes wrong raises, because a topic that is
    configured and not working is a fact worth a log line.
    """
    if not topic_arn:
        return False
    try:
        sns.publish(TopicArn=topic_arn, Subject=clean_subject(subject), Message=body)
    except Exception as exc:                                       # noqa: BLE001
        raise NotifyError(f"SNS publish to {topic_arn} failed: {exc!r}") from exc
    return True
