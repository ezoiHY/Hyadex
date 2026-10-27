# Pick up a native Codex sign-in without restarting HyadexServer

After signing in again with the native CLI (`codex login` on the server, as the
user that runs HyadexServer), subsequent Hyadex work can use a fresh Codex
app-server process. Existing chat IDs and native thread IDs are retained.
HyadexServer does not log in, log out, write credentials, copy tokens into
another authentication mode, or replay a previously submitted turn. It has no
in-app sign-in, account or API-key routes and no manual reload; the automatic
handoff below is how a new login reaches existing chats.

## Triggers and limits

- Ordinary manager lookup and turn admission compare a bounded, process-private
  revision from the child environment's `CODEX_HOME` (or `HOME/.codex`). For
  file-backed authentication, API-key changes, account changes, and a changed
  OIDC `auth_time` can request a handoff. Only a keyed digest is retained; it is
  not logged or returned through an API and is never authentication evidence.
- File timestamps, `last_refresh`, rotating access/refresh tokens, and ID-token
  issue/expiry times are excluded. A rewrite or ordinary token renewal should
  not cause process churn. Missing, malformed, oversized or unsupported data
  is inconclusive, not proof of logout or a new login.
- **Recheck CLIs** and automatic catalog refreshes
  (`GET /api/runtime/catalog?refresh=true`) refresh discovery. They can also
  observe a genuine file-backed login change, but an unknown revision never
  requests a handoff. This also protects older clients that use the query
  during reconnect or server-instance changes.
- Automatic detection is conservative and is not a complete native credential
  resolver. User configuration selects file detection; managed requirements
  may select another store. A file change is only a hint to load a fresh native
  process, which remains responsible for effective policy and credential choice.
  The server does not read Keychain secrets. `auto`, `keyring`, unknown formats,
  environment authentication and process-only `ephemeral` credentials have no
  automatic file detection. With those stores, a new login reaches Hyadex chats
  only when a fresh Codex app-server starts, for example after a server
  restart. An ephemeral login cannot be recovered from an unrelated CLI process.

Handoff logs distinguish server-owned sources (`catalog_refresh`,
`turn_admission`, `manager_lookup`), the observed-change reason, and blockers
such as background terminals, goals, pending ownership or failed unload
verification. Repeated identical blockers are coalesced per manager/chat. No
credentials, revision digests, account identities, command bodies or credential
paths are logged.

Official [authentication guidance](https://learn.chatgpt.com/docs/auth) describes
the native stores and managed overrides. The documented
[`account/read` refresh flag](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
refreshes managed OAuth tokens; it does not promise to reload every out-of-band
login. This implementation does not use that flag as a reload workaround.

## Ownership and admission

On a changed login, normal Codex managers stop receiving new unowned work.
Already-owned requests keep their exact manager, including pending non-turn
requests and the gap between acquiring a caller lease and writing a request.
A fresh chat can use a new process while another chat finishes on the old one.

Idle chat migration runs under its lifecycle lock. It checks caller leases,
callbacks, registered tasks, native turns, approvals, goals, subagents, side
chats and background terminals. Native metadata and unsubscribe work share an
eight-second budget. The old thread must be verifiably unloaded before its
local owner mapping is released; its persisted provider ID is unchanged.
Metadata, unsubscribe or spawn failure does not silently restore stale
credentials or restart a shared process.

A new ordinary turn checks this before its admission/acceptance. Busy messages
can follow the existing queue path; queue promotion repeats the check. A
temporarily blocked handoff returns a retryable 409 rather than sending new
work with a known superseded login. Existing approval/Stop controls and goal
pause/clear remain tied to the old owner so users can settle the work blocking
migration. Native background terminals started by the old process block
migration until they finish; HyadexServer no longer lists or cleans them up.

A login change expires cached readiness, including a previous login denial.
Late success/failure from a superseded normal process cannot overwrite the
current sign-in diagnostic. The native runtime must supply fresh evidence.

Retired processes close only after their owners, requests, callbacks and turns
have settled, with a second check after acquiring the process start lock.
Shutdown can still find and close them. There is no recurring login poll.
Native account switching may change workspace access; native authentication
errors remain visible. Retaining a running process cannot prevent a provider
from revoking its credentials.

## Verification boundary

Isolated regressions cover revision parsing, ordinary renewal, same-account
login signals, account switches, pending resumes, caller leases, start-lock
races, idle/queued/busy chats, goals, side chats, subagents, background
terminals, unsubscribe proof, cancellation and failed replacement. Existing
native lifecycle, authentication and client contracts are checked too.

Original handoff validation: 621 focused server tests and 43 existing desktop
runtime-catalog/health tests passed, with production-module compilation and
diff checks. An earlier broader run hit the existing force-send test's
one-second startup timeout; the final complete focused run passed that case.

A disposable real HTTP server and native Codex 0.156.1, using synthetic
file-backed API-key data and disabled external connectivity, showed cached
`apiKey` metadata after fixture removal, `none` after an explicit recheck, and
`apiKey` after adding a new fixture login. HyadexServer stayed running throughout.
This was metadata verification, not a valid account, OAuth renewal or model turn.
It used the account route and explicit recheck that have since been removed.

Before declaring ready, use a disposable real chat through the actual app:
remember a phrase, re-login with the same native account, and continue the
same thread while a second chat remains active. Separately exercise account
switching and a keychain-backed login. Verify no duplicate turn, unchanged
provider ID/history, and truthful pending/failure states. These live
OAuth/account-switch/client checks remain unverified.

### Catalog-refresh separation validation

The follow-up fix passed 67 focused server tests and 527 desktop tests, plus
desktop typecheck and production build. HTTP regressions covered repeated
forced catalog refreshes with an opaque login and owned background terminals,
continued ownership protection, native-only authorization, and redacted
source/waiting logs.

An isolated installed service was exercised using the production desktop
transport (only the Electron log-directory lookup was substituted for headless
execution). Five forced catalog refreshes and two real native Codex model turns
passed; the follow-up retained the same provider thread. The account GET,
explicit reload POST and browser/bearer rejection checked in that run covered
routes that have since been removed. No native credentials were changed; no
running training jobs were used. The opaque-login/background-work combination
is covered by isolated regressions, not live keychain/long-training acceptance.
Actual App clicking remains pending because computer control was unavailable.
