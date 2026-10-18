# Privacy and retention

Scout is designed for one owner. It stores only the opportunity fields, source
health, scores, feedback actions, notification metadata, execution metrics and
configuration required to prevent duplicate work. It does not read a personal
mailbox, collect social credentials, store complete correspondence, or publish
preferences/feedback/scores.

Local `data/` contains SQLite state and exports. It is ignored by Git and should be
protected by the operating-system account. The optional `scout-state` branch in the
separate private state repository contains compressed JSON state for recovery; Git
history is also private state. Do not enable Pages, public artifacts or public
repository state for it.

Default retention is 180 days for old opportunities subject to the outbox and 1,000
recent run records. Tombstones and notification idempotency keys are retained to
prevent re-reporting a removed opportunity. Unsent notification references are
protected. Change `retention_days`/`max_opportunities` only after checking storage
growth and the need for old evidence.

Source pages can contain personal names or public organizer contact details. Scout
retains bounded extracted summaries/evidence excerpts, not entire pages indefinitely.
Use the source's terms and applicable privacy law. Never add private newsletters,
student records or personal correspondence without a deliberate, documented source
policy.

To remove local state, stop all Scout processes, export only if needed for audit, and
delete `data/` plus any private checkpoint branch through the normal GitHub UI. That
destructive action is not automated by the CLI.
