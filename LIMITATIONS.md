# Limitations

## Harness versions and wrappers

Live steering requires Pi 0.87.1 or newer, or Claude Code 2.1.283 or newer with `msg_lifecycle_v1` and plural result/input associations.
Hive checks the binary and protocol at startup and reports unsupported configurations in task details.
Older print-only wrappers are not a fallback for live steering.
Wrappers must forward bidirectional stdin/stdout, separate stderr, and `--version` without buffering away protocol records.

Hive owns protocol and session flags, including Pi's explicit `--session-id` support.
Remove conflicting mode, print, or session-selection options from configured harness/template arguments.

## Pi message spacing

Pi 0.87.1 has been observed acknowledging several prompts sent together without adding all of them to its conversation, both at startup and after becoming idle.
Hive therefore uses its existing queue to send user messages to each Pi process at least one second apart, measured from completion of the preceding message write.
The first message is not delayed, and protocol control commands, reading responses, and cancellation are not delayed by this interval.
Claude Code does not use this interval.

This is a small workaround for third-party behavior, not a guarantee of delivery or processing.
Ten isolated tests with Pi 0.87.1 and `openai-codex/gpt-5.6-sol` retained all messages in order: five starting with a fresh session and five sending follow-ups after Pi became idle.
Other timings, configurations, or versions may still lose messages.
Pi's successful command response confirms acceptance only; Hive does not infer per-message processing from it or automatically resend uncertain inputs.
Messages submitted in a burst wait in Hive's queue, so later messages take longer to reach Pi.

## Delivery and cleanup

Acceptance is not an assistant answer, and multiple steering messages can share one result.
Pi does not expose exact input-ID-to-reply attribution; Hive does not invent it.
Unknown events produce a reduced-fidelity warning.
Uncertain inputs after a disconnect are not automatically resent.

Cancellation stops the shared conversation run, not an individual instruction already incorporated into context.
Cleanup covers the harness and children remaining in its process group, not independently detached services.

See [Live steering](LIVE_STEERING.md) for protocol details, error visibility, diagnostics, and validation commands.
