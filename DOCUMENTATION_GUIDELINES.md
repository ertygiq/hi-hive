# Documentation guidelines

## Audience

The README is a short product introduction and quick start for users who may not be deeply technical.
Use plain language and include only the detail needed to understand, install, and begin using hi-hive.

## README

Keep the README lightweight and easy to scan.
State platform support with short labels such as supported, experimental, or not supported.
Do not put CI policy, service-manager internals, architecture matrices, detailed distro coverage, or troubleshooting in the README introduction.
Link to focused documentation when readers need technical detail.
Keep setup examples short and avoid explanatory comments when the same command works across platforms.

## Detailed documentation

Put operating-system setup, background services, SSH access, logs, limitations, and troubleshooting in `docs/setup.md` or another focused document.
Put contributor testing and CI details in development documentation or workflow files rather than the user-facing introduction.
When a feature needs qualification, use a short status in the README and explain the exact scope in the detailed documentation.

Update this file when the project's documentation audience, structure, or level-of-detail decisions change.
