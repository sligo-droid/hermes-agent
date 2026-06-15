# GitHub Mention → PR Amendment Webhook

Status: implemented in branch `github-pr-amend-webhook`; awaiting merge + external GitHub webhook setup  
Owner: Sligo Labs agent  
Created: 2026-06-15  
Updated: 2026-06-15  
Initial canary: `reserve-protocol/reserve-index-dtf#182`

## Goal

Let the trusted GitHub user `tbrent` tag the bot identity `@sligo-droid` in a pull request comment, inline review comment, or submitted review, then have Hermes amend that PR directly:

1. receive a signed GitHub webhook;
2. deterministically verify that the event is allowed;
3. collect the relevant PR/review context, including Changes Requested / review feedback;
4. create a bounded coding job;
5. check out the PR head branch in an isolated workspace;
6. implement the requested code change;
7. run focused verification;
8. commit and push to the PR head branch;
9. reply in GitHub with the result, commit SHA, tests run, and any caveats.

This is not a generic public GitHub chat surface. It is a trusted-author PR amendment channel for the `sligo-droid` bot identity.

## Non-goals

- Do not create or require a GitHub App for v1.
- Do not poll GitHub.
- Do not allow arbitrary GitHub users to trigger coding work by tagging the bot.
- Do not merge PRs, approve reviews, request reviews, deploy, publish releases, change repo settings, or send/draft/reply/forward email from this workflow.
- Do not use the existing generic webhook prompt route as the final implementation for code changes; it is intentionally safe and does not expose the editing capability this workflow needs.

## Trust and safety model

The user has stated:

- `sligo-droid` is the AI/bot GitHub identity, not the human user's account.
- The human user is `tbrent`.
- `tbrent` will only tag the bot in PRs whose content is safe for the agent to inspect and act on.
- The desired behavior is for the bot to amend the PR, not merely discuss the request.

Therefore, prompt injection defense is handled primarily by **deterministic trigger and side-effect gates**, not by treating every tagged PR as hostile. Once a request passes gates, the coding worker may inspect the PR diff, review comments, repository files, and relevant test/build outputs.

Hard gates still apply before any coding work starts:

- valid GitHub HMAC signature;
- accepted event type;
- accepted action;
- `sender.login` is allowlisted, initially only `tbrent`;
- body contains the configured mention, initially `@sligo-droid`;
- event targets an open PR;
- base repo is allowlisted;
- PR head repo is allowlisted / pushable by `sligo-droid`;
- canary PR allowlist passes while rollout is scoped;
- no active branch lock exists for the PR head branch.

The coding worker may perform only the explicit PR amendment side effects: clone/fetch/checkout, edit files, run tests/builds, commit, push to the exact PR head branch, and comment the result.

## Initial canary policy

```yaml
github_pr_amend:
  enabled: true
  route: github-pr-amend
  mention: "@sligo-droid"
  allowed_senders:
    - tbrent
  allowed_base_repos:
    - reserve-protocol/reserve-index-dtf
  allowed_head_repos:
    - sligo-droid/reserve-index-dtf
  canary_prs:
    reserve-protocol/reserve-index-dtf:
      - 182
  trigger_events:
    - issue_comment
    - pull_request_review_comment
    - pull_request_review
  allowed_actions:
    issue_comment:
      - created
    pull_request_review_comment:
      - created
    pull_request_review:
      - submitted
  allowed_side_effects:
    - checkout_pr_branch
    - edit_files
    - run_tests
    - commit
    - push_to_pr_branch
    - comment_result
  forbidden_side_effects:
    - merge
    - approve_review
    - request_review
    - deploy
    - release
    - change_repo_settings
    - email
```

After PR #182 proves the flow, remove the canary PR limit while keeping sender/base/head gates.

## GitHub events to support

### `issue_comment`

Use for top-level PR conversation comments.

Accept only when:

- `action == "created"`;
- `issue.pull_request` exists;
- `comment.body` contains `@sligo-droid`;
- `sender.login == "tbrent"`.

Context to fetch:

- PR details via `GET /repos/{owner}/{repo}/pulls/{number}`;
- issue conversation comments via `GET /repos/{owner}/{repo}/issues/{number}/comments`;
- reviews via `GET /repos/{owner}/{repo}/pulls/{number}/reviews`;
- review comments via `GET /repos/{owner}/{repo}/pulls/{number}/comments`;
- changed files / diff summary;
- status checks for current head SHA when available.

### `pull_request_review_comment`

Use for inline review comments on a file/line.

Accept only when:

- `action == "created"`;
- `comment.body` contains `@sligo-droid`;
- `sender.login == "tbrent"`.

Additional context:

- `comment.path`, `line`, `original_line`, `diff_hunk`, `commit_id`, `pull_request_review_id`;
- nearby file content after checkout;
- other comments in the same review when available.

### `pull_request_review`

Use for submitted reviews, including Changes Requested.

Accept only when:

- `action == "submitted"`;
- `review.body` contains `@sligo-droid`;
- `sender.login == "tbrent"`.

Additional context:

- `review.state` (`changes_requested`, `commented`, `approved`);
- all comments associated with `review.id`;
- PR review decision and outstanding feedback.

A future mode may allow `changes_requested` by `tbrent` to trigger without an explicit mention, but v1 requires the mention.

## Runtime architecture

### Ingress

Add a dedicated GitHub PR amendment route instead of overloading the generic prompt webhook route. The existing webhook adapter already handles HTTP serving, HMAC validation, rate limiting, and idempotency; v1 can either:

1. extend the webhook adapter with a specialized `github_pr_amend` route mode, or
2. add a small dedicated gateway platform/helper that mounts a route and dispatches a job.

The implementation should reuse existing primitives where possible but keep the GitHub PR amendment policy explicit and testable.

### Job dispatch

Webhook handling must return quickly. It should enqueue or start a bounded background job and then release the HTTP request. The job should run under Hermes-controlled background process / worker orchestration with an auditable local record.

Preferred behavior:

- acknowledge accepted webhook quickly;
- optionally post a short GitHub acknowledgement comment, e.g. “Queued, I’ll amend this branch if verification passes.”;
- run the amendment job out of band;
- post final result comment.

### Branch lock

Use a lock key derived from the PR branch identity:

```text
{head_repo}:{head_ref}
```

For PR #182:

```text
sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients
```

If the lock is held, either queue the new request or reply that another amendment is already in progress. Do not run two concurrent coding workers against the same branch.

### Workspace

Use isolated workspaces under `/home/droid/workspaces/`, not canonical checkouts. A suggested path:

```text
/home/droid/workspaces/github-pr-amend/{owner}-{repo}-pr-{number}
```

For each job:

1. create/reuse workspace;
2. fetch the PR head repo/ref;
3. reset checkout to the event's current PR head SHA before editing;
4. add/fetch upstream base repo for context;
5. verify `git status --short` is clean before edits;
6. run the coding worker;
7. verify `git status --short` after edits;
8. commit and push only to the exact head repo/ref.

### Worker contract

The coding worker receives a structured brief:

- allowed side effects;
- PR URL and repo/ref metadata;
- triggering event type and GitHub URL/comment ID;
- trusted request text from `tbrent`;
- PR title/body;
- current head SHA;
- relevant review state and comments;
- changed files and diff summary;
- explicit forbidden actions;
- required verification expectations.

The worker must return:

- summary of changes;
- files changed;
- commands/tests run and outputs summarized;
- commit SHA if pushed;
- blocker details if no commit was pushed.

### Final GitHub comment

On success:

```text
Implemented in <sha>: <short summary>

Verification:
- <command>: <result>

Notes:
- <caveats if any>
```

On failure:

```text
I tried to apply this but hit a blocker. No commit was pushed.

Blocker:
- <specific issue>

Verification attempted:
- <commands/results>
```

On policy rejection for unauthorized users, prefer no public reply at first; log the skip locally.

## GitHub credentials

Use the `sligo-droid` GitHub identity for:

- reading PR metadata/comments/reviews;
- cloning/fetching;
- pushing to `sligo-droid` branches;
- commenting back on PRs.

A narrower fine-grained token is preferable later, but v1 can use the existing authenticated `sligo-droid` CLI/runtime credentials if that is the current operational path.

## Implementation status

Implemented pieces in this branch:

- new `gateway/github_pr_amend.py` policy/helper module;
- specialized webhook route mode: `mode: github_pr_amend`;
- deterministic preflight gates before PR metadata lookup: event action, sender, mention, base repo, canary PR;
- PR metadata gates after lookup: open PR, allowlisted base/head repo, non-empty head ref;
- in-process branch lock keyed as `{head_repo}:{head_ref}`;
- auditable job brief written under `$HERMES_HOME/github-pr-amend/...`;
- bounded one-shot Hermes worker command with explicit toolsets, max turns, quiet mode, and yolo approval bypass for headless execution;
- worker prompt contract that fetches current PR reviews/comments/diff, edits only the exact PR head branch, verifies, pushes, and comments back;
- failure/crash GitHub comments if the worker exits non-zero after accepting a request.

Verification run on 2026-06-15:

- `scripts/run_tests.sh tests/gateway/test_webhook_adapter.py tests/gateway/test_webhook_integration.py tests/gateway/test_github_pr_amend.py -v --tb=short` → `85 passed`;
- `scripts/run_tests.sh` smoke suite → `767 passed, 8 warnings`;
- local runtime check with loopback webhook adapter on `127.0.0.1:8644`:
  - `GET /health` → `200 {"status": "ok", "platform": "webhook"}`;
  - unauthorized `issue_comment` fixture → `200 {"status": "ignored", ... "sender 'stranger' is not allowlisted"}`;
- Cloudflare Tunnel ingress dry-run config for `sligo.sligolabs.com` path routing validated with `cloudflared tunnel ingress validate` → `OK`.

Broad `scripts/run_tests.sh tests/gateway/` is not a useful gate for this branch right now: it runs 6,532 tests and currently has unrelated platform/config failures outside the webhook surface. The fork smoke suite and focused webhook/gateway tests above are the relevant merge gates for this change.

## Webhook setup for GitHub

The human/operator must add the webhook because `sligo-droid` currently has read-only access to `reserve-protocol/reserve-index-dtf` settings.

GitHub repo settings:

- Payload URL: `https://sligo.sligolabs.com/webhooks/github-pr-amend`
- Content type: `application/json`
- Secret: generated strong shared secret matching Hermes config
- SSL verification: enabled
- Events: select individual events:
  - Issue comments
  - Pull request review comments
  - Pull request reviews

That URL assumes the existing Cloudflare Tunnel for `sligo.sligolabs.com` gets a path-specific ingress rule before the dashboard fallback:

```yaml
ingress:
  - hostname: sligo.sligolabs.com
    path: /webhooks/github-pr-amend
    service: http://127.0.0.1:8644
    originRequest:
      httpHostHeader: 127.0.0.1:8644
  - hostname: sligo.sligolabs.com
    service: http://127.0.0.1:9119
    originRequest:
      httpHostHeader: 127.0.0.1:9119
  - service: http_status:404
```

Hermes config shape for the route after this branch is merged/deployed:

```yaml
platforms:
  webhook:
    enabled: true
    host: 127.0.0.1
    port: 8644
    routes:
      github-pr-amend:
        secret: "<same strong secret configured in GitHub>"
        events:
          - issue_comment
          - pull_request_review_comment
          - pull_request_review
        mode: github_pr_amend
        github_pr_amend:
          mention: "@sligo-droid"
          allowed_senders:
            - tbrent
          allowed_base_repos:
            - reserve-protocol/reserve-index-dtf
          allowed_head_repos:
            - sligo-droid/reserve-index-dtf
          canary_prs:
            reserve-protocol/reserve-index-dtf:
              - 182
          job:
            hermes_command: hermes
            toolsets: terminal,file,web,session_search
            max_turns: 120
            timeout_seconds: 1800
            workspace_root: /home/droid/workspaces/github-pr-amend
```

Current live state at the time of writing: the route is **not active** on the live gateway yet. `platforms.webhook` is empty in the active Hermes config, and the existing Cloudflare Tunnel routes all `sligo.sligolabs.com` traffic to the dashboard on `127.0.0.1:9119`. After merge/deploy, the remaining operator actions are: enable this route in Hermes config, add the Cloudflare path ingress above, restart the gateway/tunnel, then add the GitHub webhook using the exact Payload URL above.

A public HTTPS route to the Hermes gateway is required, such as Cloudflare Tunnel or a reverse proxy.

## Verification plan

Unit tests:

- accepts `issue_comment.created` from `tbrent` with mention on PR #182;
- rejects missing mention;
- rejects non-`tbrent` sender;
- rejects non-PR issue comments;
- accepts `pull_request_review_comment.created` with mention;
- accepts `pull_request_review.submitted` with mention and `changes_requested`;
- rejects events outside allowed repo/head repo/canary PR;
- lock prevents concurrent jobs for same head branch;
- final comment formatting includes commit/test/blocker fields.

Focused integration/smoke tests:

- signed webhook request reaches route and returns accepted;
- policy engine extracts PR number/event context correctly;
- dry-run job builds the expected checkout/push plan without pushing;
- mocked GitHub API responses produce a complete worker brief.

Runtime verification before enabling upstream webhook:

- gateway starts with route configured;
- `GET /health` responds;
- local signed POST to `/webhooks/github-pr-amend` with a fixture payload is accepted/rejected correctly;
- logs show accepted/skipped reasons without secrets;
- route URL and secret location are known.

## Rollout

1. Land implementation behind config flag, disabled by default.
2. Enable locally for canary policy and route.
3. Give operator exact webhook URL and event list.
4. Operator adds webhook in GitHub settings.
5. Test with a harmless tag on PR #182.
6. Confirm branch amendment + final comment.
7. Remove PR #182 canary if desired.

## Open questions / implementation choices

- Whether to represent accepted webhook jobs as Command Center/Kanban work items in v1 or start with a simpler background job record and add Command Center integration after the core path works.
- Whether to post an immediate “queued” acknowledgement comment or stay silent until final result.
- Whether to allow non-PR allowlisted branches after the PR #182 canary.

Recommendation: start with a simple background job plus branch lock and final comment, then add Command Center/Kanban surfacing once the core end-to-end path is proven.
