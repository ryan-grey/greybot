# Verification help

The “Get verification help” button on the permanent start message and future welcome messages replies privately to the member who clicks it. It provides troubleshooting, not automatic verification or a staff ticket. The verification page also includes the same help without requiring administrator access.

## Staff checklist

1. Confirm the member accepted Discord's server rules; check whether membership screening is still pending.
2. Confirm the account shown on the verification page is the account that joined the server.
3. For an expired or failed human check, reload and complete a new challenge; never ask for passwords, session links, or challenge tokens.
4. For accepted verification with no role, inspect the member's verification job in the admin workspace and confirm the configured role, bot hierarchy, worker health, and recorded result. Do not bypass the human check by granting access solely on a support request.
5. If the role is present, check effective channel access and have the member open Channels & Roles → Browse Channels. Verification does not grant raid or staff roles.

## Activation

Deploy the help interaction handler before adding its button to the existing permanent start message. Preserve that message's current verification button and content; append `greybot:verification_help` as a secondary button. Read back the components and validate a signed interaction returns an ephemeral response. Keep staff logs private. Do not post an admin-workspace link in the public start or welcome message.
