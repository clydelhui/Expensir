# Pending tracker updates (policy-denied for the agent — please run these)

Closing issues not created in-session is denied by the permission classifier, so the
implementation notes are parked here. Delete each entry (or this file) after posting.

## Close #3 (implemented in a927847)

```
gh issue close 3 --comment-file docs/agents/issue-3-close-comment.md
```

## Close #4 (implemented in 2744c9c)

```
gh issue close 4 --comment-file docs/agents/issue-4-close-comment.md
```

## Comment on #19 (poll-loop hardening, carried over from slice 1 review)

`run_poll` has no backoff/error handling (an instant server means ~5k getUpdates/4s;
one exception kills the process), and it has **no dedupe at all**: a crash between a
successful dispatch and the offset confirm re-applies already-committed mutations on
restart (same class as the webhook double-apply fixed in slice 2 — see
`tests/test_webhook.py::test_send_failure_after_commit_never_double_applies_the_mutation`).
Slice 18 should add offset persistence or ProcessedUpdate reuse plus backoff/logging.
