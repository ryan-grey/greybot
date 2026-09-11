# Permanent onboarding channel identities

Configure these values in the private runtime environment for both web and worker:

- `GREYBOT_START_CHANNEL_ID`: the permanent ID of the verification entry channel.
- `GREYBOT_PUBLIC_CHANNEL_IDS`: comma-separated permanent IDs of public entry channels, including the guide and channel-preferences channel.

The configured welcome channel and Discord's designated server rules channel are also protected where appropriate. Public entry IDs control the onboarding permission planner and exclusion from self-service channel hiding. The start ID controls the link in future welcome posts. Channel names are display labels only: moving a channel, adding emojis, or renaming it does not require a deployment or change the configured ID. A newly created channel requires deliberate configuration; copying an old name does not inherit special access.

Configure these IDs before applying an onboarding permission plan. Keep actual server configuration outside the public checkout.
