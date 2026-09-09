# Seance threat model

This threat model names each mitigation, the module that implements it, and the
test(s) that prove it. Test names are real functions in `tests/`; run
`.venv/bin/python -m pytest -q` to exercise them.

Trust boundaries:

- **TLS terminates at Caddy**; the seance process serves plain HTTP/WS on
  `SEANCE_BIND` and trusts the network only for forwarding headers from
  configured `trusted_proxies` (§ "XFF-first" below).
- **Identity is server-derived only.** Nothing identity-bearing is trusted from a
  request body or an unstamped envelope field (`app/identity.py`,
  `app/protocol.py` `validate_message` + `stamp`).
- **Server identity records contain no credentials.** They retain only `user_id`
  / username material, without IP addresses, email addresses, cookies, or tokens
  (`app/store.py` module docstring).
- **Browser identity is a bearer capability.** The SDK retains anonymous tokens
  in the application's per-tab `sessionStorage`, scoped to the Seance base URL,
  so cross-site clients retain ownership after reload. Same-origin application
  scripts can read that token; it never enters a share URL. Hosts can disable or
  replace this storage with `anonTokenStorage` (see `sdk/README.md`).

---

## 1. Threat → mitigation → proof

| Threat | Mitigation | Module(s) | Proving tests |
|---|---|---|---|
| Identity spoofing (forged `username`/`user_id`) | All identity server-derived; the server overwrites the seven envelope fields on every outbound frame; `validate_message` drops client-supplied envelope/unknown keys; `X-GS-*` read only from a gs-trusted socket peer | `identity.py`, `protocol.py` (`stamp`, `validate_message`), `httpapi.py` (`ticket`) | `test_transport.py::test_two_clients_converge_on_state_update` (relay carries the acting identity), `test_httpapi.py::test_ticket_untrusted_peer_forbidden`, `test_protocol.py` validators |
| Cookie theft / replay | gs's own posture mirrored byte-for-byte: Fernet signature + TTL + client-IP binding; tickets single-use, 120 s; anon tokens are a session-scoped capability revocable by ban | `identity.py` (`GsSerializer`, `validate_gs_cookie`, `redeem_ticket`) | `test_identity.py::test_validate_gs_cookie_wrong_ip`, `::test_validate_gs_cookie_expired`, `::test_ticket_single_use`, `test_transport.py::test_owner_ban_blocks_rejoin_4403` |
| CSWSH (evil site opens a WS with victim cookies) | Origin allowlist enforced on the WS handshake and on `/v1` HTTP; anon cookie is `SameSite=Lax` | `transport.py` (`websocket_handler`), `httpapi.py` (`_cors_mw`, `_set_anon_cookie`) | `test_transport.py::test_bad_origin_rejected_pre_upgrade`, `::test_missing_origin_rejected_pre_upgrade`, `test_httpapi.py::test_cors_disallowed_origin_post_forbidden` |
| Frame floods / op storms | Per-connection, per-lane token buckets; a held-exhausted lane closes `4429` after `abuse_window` | `ratelimit.py` (`LaneLimiter`), `transport.py` (`_run_read_loop`) | `test_transport.py::test_rate_limit_error_then_sustained_flood_closes_4429`, `test_ratelimit.py::test_bucket_burst_then_refusal`, `::test_lane_exhausted_since_tracks_first_refusal` |
| Oversize-payload DoS | Per-frame/per-field caps plus an 8 MiB aggregate persisted-content budget; each growing state/data/poly/doc/chat mutation is applied to a component candidate and committed only if the exact compact-JSON total fits; creator snapshots and thawed rows are checked before admission | `protocol.py`, `transport.py`, `engine.py`, `session.py`, `hub.py` | `test_transport.py::test_oversize_nonsnapshot_frame_closes_after_violations`, `test_session.py::test_state_update_rejects_aggregate_session_budget_without_mutating`, `::test_data_update_rejects_aggregate_session_budget_without_mutating`, `::test_poly_upsert_rejects_aggregate_session_budget_without_advancing_rev`, `::test_chat_message_rejects_aggregate_session_budget_without_echo`, `test_doc_integration.py::test_doc_create_rejects_aggregate_session_budget_without_mutating`, `test_hub.py::test_create_rejects_snapshot_over_aggregate_session_budget` |
| Anonymous persistence exhaustion | Anonymous creates are limited independently by resolved IP before identity-based quotas; the global capacity check counts every persisted row and is serialized with insertion | `httpapi.py`, `hub.py`, `store.py` | `test_httpapi.py::test_create_session_anon_is_rate_limited_per_ip_without_cookies`, `test_hub.py::test_create_server_full_counts_frozen_rows`, `::test_create_capacity_check_and_insert_are_serialized` |
| Multiple processes corrupt live ownership | A file-backed store holds `LOCK_EX|LOCK_NB` on a mode-`0600` `<SEANCE_DB>.lock` descriptor for its lifetime; a second process fails startup | `store.py` | `test_store.py::test_file_backed_store_has_one_process_owner` |
| Slow-loris / slow consumer | 10 s `hello` deadline; heartbeat reap; bounded per-connection send queue that sheds cursors then closes `4408` | `transport.py` (`_read_hello`, `_run_heartbeat`, `WsConn`) | `test_transport.py::test_wsconn_sheds_cursors_then_closes_slow_consumer`, `::test_pre_hello_frame_closes_4400`, `::test_heartbeat_keeps_connection_alive` |
| Session-id guessing | 62⁶ ≈ 5.7×10¹⁰ uniform-random ids; per-IP join and probe rate limits; existence disclosure via `GET /v1/sessions/{id}` is deliberate (share-link UX) and rate-limited | `hub.py` (`_new_session_id`), `httpapi.py` (`session_probe`) | `test_httpapi.py::test_session_probe_rate_limited`, `::test_session_probe_unknown_not_found`, `test_transport.py::test_join_rate_limit_429_pre_upgrade` |
| Cross-session data leakage | Session objects are fully isolated; no shared mutable state between sessions | `hub.py`, `session.py` | `test_integration_scenarios.py::test_i_session_isolation_no_crosstalk` |
| Malicious owner | Owner powers are scoped to their own session; every verb audits; the creator-reclaim rule limits hostile transfers (see §2.1) | `session.py`, `moderation.py` | `test_moderation.py::test_every_successful_verb_audits_and_broadcasts_once`, `test_session.py::test_owner_ladder_creator_leaves_then_reclaims` |
| Ban evasion (anon re-mint) | Documented residual; owner dials (`mod-guests` off, `mod-lock`) are the mitigation; no IP persistence by design (see §2.2) | `session.py` (`join`), `moderation.py` | `test_moderation.py::test_guests_toggle_boots_nobody_but_blocks_new_anon`, `::test_lock_toggles_and_blocks_new_joins` |
| Injection via document content | The server never interprets DSL/text — node/program/param text is opaque; strict JSON validation per type; all SQL parameterized | `protocol.py` (validators), `engine.py` (opaque text), `store.py` | `test_protocol.py` validators, `test_engine_poly.py::test_node_fields_exact`, `test_store.py::test_save_load_round_trip` |
| Secret handling | Keys read only from env or `*_FILE` secret paths; startup fails fast on a missing/malformed key; keys never logged; no default keys | `config.py` (`_require_fernet`, `_optional_fernet`) | `test_config.py::test_missing_secret_raises_naming_it`, `::test_malformed_fernet_key_raises`, `::test_secret_file_beats_inline` |
| Log/config privacy | IPs live only in RAM; audit records are identity-only; no message bodies at info level; unexpected HTTP errors use route templates rather than raw paths/session ids; malformed service-secret entries are redacted | `audit.py`, `store.py`, `clientip.py`, `httpapi.py`, `config.py` | `test_store.py::test_log_audit_emits_compact_sorted_json`, `test_httpapi.py::test_unhandled_error_log_uses_route_template_not_session_id`, `test_config.py::test_malformed_service_secret_error_redacts_raw_entry` |
| Stored session over-retention | Frozen sessions are deleted by the in-process TTL sweep when `FROZEN_SESSION_TTL > 0`; each candidate is rechecked under the per-session thaw/freeze lock so racing live session ids are skipped; audit rows remain retained | `hub.py` (`scan`, `_delete_frozen_if_still_cold`), `store.py` (`list_frozen_older_than`, `delete_session`) | `test_doc_integration.py::test_scan_deletes_frozen_sessions_older_than_ttl`, `test_hub.py::test_retention_delete_does_not_orphan_racing_thaw`, `test_store.py::test_delete_session` |

---

## 2. Adjudicated decisions and residual risks

These are the decisions reviewers ratified during the build, each with its exact
code behavior and any residual the operator must account for.

### 2.1 Owner supremacy

The **acting owner** is a user-level role recomputed from the present roster on
every join/leave/transfer (`session.py` `_compute_owner`). Precedence:

1. an explicit transferee (`settings.explicit_owner`) **if present**, else
2. the **creator** (`created_by`) **if present**, else
3. the earliest joiner still in the room.

**Acting-owner vs. returning creator.** Because rule 2 beats rule 3, a returning
creator **reclaims** ownership from an acting owner who only held it by
earliest-join order — *unless* a present explicit transferee holds it (rule 1),
which suppresses the reclaim until that transferee leaves. This is the
creator-reclaim rule used by the session owner model.
Proof: `test_session.py::test_owner_ladder_creator_leaves_then_reclaims`,
`test_moderation.py::test_transfer_suppresses_creator_reclaim_until_owner_leaves`,
`test_integration_scenarios.py::test_c_owner_handoff_and_creator_reclaim`.

**Self-targeting.** An owner **cannot** kick or ban **themselves**, and cannot
set their own readonly **on** — each rejects a self target with
`error {code:"forbidden"}` (`moderation.py` `_ban`, `_resolve_kick_target`,
`_readonly`). The one permitted self-target is `mod-readonly {readonly:false}`
(self-*lift*), detailed in the readonly-owner note below.
Proof: `test_moderation.py::test_owner_cannot_ban_or_kick_self`,
`::test_readonly_cannot_target_self`.

**Transfer-to-self is accepted** (adjudicated). `mod-transfer {target_user:
<self>}` is *not* rejected: it sets `explicit_owner` to the owner's own id,
recomputes the owner (no change, so no `owner-changed` is emitted), and still
audits and broadcasts a `moderation` event (`moderation.py` `_transfer`). This is
intentional owner supremacy — an owner reasserting ownership is a safe no-op that
can never cost them the room. Proof:
`test_moderation.py::test_transfer_to_present_member_emits_owner_changed` covers
the present-target path;
`::test_transfer_absent_or_ephemeral_forbidden` shows the only transfer targets
that are rejected (absent or `gs_ephemeral`).

**Readonly-owner self-lift (implemented).** A user can be placed in
`readonly_users` while a non-owner and then *become* owner (e.g. the previous
owner leaves and this user is next in the ladder). Such an owner is read-only for
**write** frames, so `mod-readonly` **allows a self target with `readonly:false`**
— the owner clears their own read-only flag — while a self target with
`readonly:true` stays forbidden, so an owner can never lock *themselves* out of
writing (`moderation.py` `_readonly`, lines 76-89). Proof:
`test_moderation.py::test_readonly_owner_can_lift_own_readonly` (self-lift
succeeds and writes flow again afterward), `::test_readonly_cannot_target_self`
(self-*set* is still rejected). The gate is bounded regardless: read-only touches
only `WRITE_TYPES` (`session.py` `handle`), so a read-only owner **retains full
moderation authority** (`mod-*` are owner-gated, not readonly-gated) and could
also `mod-transfer` to another eligible participant. `gs_ephemeral` identities can
never be owner (`_compute_owner` skips them), so this only touches member/anon
owners.

### 2.2 Anon ban-evasion residual and owner dials

A ban is keyed on `user_id` and persisted in the `bans` table across freeze/thaw
(`moderation.py` `_ban`, `store.py` `add_ban`; `session.py` `join` refuses a
banned `user_id` with `4403`). A banned **anonymous** user can mint a *fresh*
anon identity (a new random `user_id`) and rejoin: the ban does not follow,
because **IP addresses are never persisted**. This privacy-preserving residual
is acknowledged, not fixed with IP bans.

The mitigation is owner dials, not IP tracking:

- `mod-guests {allowed:false}` — blocks **all** new guest (anon/gs_ephemeral)
  joins while leaving present users seated
  (`test_moderation.py::test_guests_toggle_boots_nobody_but_blocks_new_anon`).
- `mod-lock {locked:true}` — blocks **all** new joins with `4423`
  (`test_moderation.py::test_lock_toggles_and_blocks_new_joins`).
- `mod-readonly` (per-user) and the `guests_readonly` setting restrict *writes*
  without blocking joins (`session.py` `_is_readonly`).

Members do not have this evasion: a member's `user_id` is their stable gs id, so
a member ban sticks across reconnects.

### 2.3 XFF-first client-IP resolution (gs parity) and its spoofability

`clientip.py` `resolve_client_ip` honors forwarding headers **only** when the
direct socket peer is inside `trusted_proxies`; otherwise it returns the raw peer
verbatim. When trusted, precedence is **`X-Forwarded-For` (first entry)** →
`X-Real-IP` → RFC 7239 `Forwarded` `for=` → peer. This order is **byte-parity
with groundsquirrel's `client_ip.py`** and must not diverge: gs `SESSION` cookies
bind to the resolved client IP, so a different resolution would silently reject
otherwise-valid member cookies (`identity.py` `validate_gs_cookie` IP check).
Proof: `test_clientip.py::test_untrusted_peer_ignores_forwarding`,
`::test_trusted_peer_takes_first_xff_entry`,
`::test_xff_precedence_over_real_ip`,
`::test_empty_trusted_proxies_returns_peer`.

**Spoofability posture.** "XFF-first" trusts whatever the trusted proxy places in
the first `X-Forwarded-For` entry. That is safe **when Caddy is the only trusted
proxy and appends the real socket peer to `X-Forwarded-For`** rather than
passing a client-forged header through unmodified, so the first entry seen behind
Caddy is authoritative. The
**trusted-proxy gate is the security boundary**: a direct client that is not in
`trusted_proxies` has all forwarding headers ignored and cannot spoof its IP.

Residuals to manage at deploy time:

- Keep `SEANCE_TRUSTED_PROXIES` **tight** — only the real proxy's network. A
  too-broad set lets a host inside it forge the client IP, which would evade the
  per-IP rate limits (anon mint, join, session-create) and could satisfy a gs
  cookie's IP binding for a stolen cookie.
- Do not front seance with a proxy that passes a client's `X-Forwarded-For`
  through **without** appending/overwriting; the first-entry trust then becomes
  client-controlled.
- `trusted_proxies` empty (the default) is fail-safe: headers are always ignored
  and the socket peer is used.

The resolved IP is used only in memory for rate limiting and gs-cookie IP
binding; it is never logged in the clear or persisted.

### 2.4 Deleted-member → anon fallthrough

When a gs `SESSION` cookie names a member whose directory record is **missing**
or **deleted** — `deleted_at` set, or username NULL/empty
(`directory.py` `_to_record`) — `validate_gs_cookie` raises
`AuthError("member not found")` (`identity.py`). On the resolve path this is
**not** a hard failure: `resolve` catches the cookie error and falls through to
the next credential, ultimately minting a **fresh anonymous identity**
(`identity.py` `resolve`). A deleted/revoked account therefore degrades to a
**signed-out visitor**, not an error — the adjudicated "intended signed-out
semantics." Proof: `test_identity.py::test_validate_gs_cookie_deleted_member`,
`::test_validate_gs_cookie_missing_member`,
`::test_resolve_invalid_cookie_mints_fresh_anon`,
`::test_resolve_invalid_cookie_falls_to_anon_token`.

Security consequence: a revoked member **silently loses member privileges**
(correct) but can still participate **anonymously** wherever guests are allowed;
the owner's guest dials (§2.2) govern that. Note the exception to fallthrough: a
**ticket** presented in `hello` is a definite auth intent and does *not* fall
through — an invalid ticket hard-fails (`identity.py` `resolve`;
`test_identity.py::test_resolve_invalid_ticket_raises_no_fallthrough`).

### 2.5 WAL sidecar file permissions

`store.py` `open` chmods the **main** SQLite database to `0600`
(`test_store.py::test_db_file_mode_0600`). SQLite in WAL mode also creates the
`-wal` and `-shm` **sidecar** files under the process umask rather than
inheriting the DB file's mode, so `store.py` `_harden_wal_sidecars` re-chmods
both to `0600` at `Store.open` (after the WAL PRAGMA/schema materialize them),
after write commits that can create or recreate sidecars, and again at
`Store.close`. At rest they therefore match the main file
(`test_store.py::test_wal_sidecar_mode_0600`,
`::test_wal_sidecars_rehardened_after_write_commit`).

**Residual — the recreation window.** SQLite can recreate a sidecar
**mid-operation** (e.g. a checkpoint folds the `-wal` away and a later write
reopens it); a freshly recreated sidecar carries the process umask until the
post-write commit hardening runs. Exposure is bounded — the sidecars carry the
same session JSON, which is **identity-only** (no IPs, tokens, or emails ever;
`store.py` docstring) — but the deployment should still close that window:

- run seance with a restrictive umask (e.g. `077`), and/or
- place `SEANCE_DB` in a directory that is itself `0700` and owned by the seance
  user (uid 10001 in the image), so the sidecars are unreadable regardless of
  their own mode.

The image creates `/data` as `0700`; mounted durable volumes should preserve the
same owner-only parent-directory property.

### 2.6 Frozen-session retention

Frozen sessions are retained for `frozen_session_ttl` seconds (default 86400.0)
and pruned by `hub.py` `scan()` when that limit is positive. The sweep lists
rows whose `frozen_at` is older than the cutoff, then takes the per-session
thaw/freeze lock, rechecks `hub.live`, and calls `Store.delete_session(id)` only
when the row is still cold. A non-positive `SEANCE_LIMIT_FROZEN_SESSION_TTL`
disables the sweep and retains frozen sessions indefinitely.

`Store.delete_session` removes the session row and its per-session bans. Audit
rows are deliberately retained as the moderation/security audit trail. Proof:
`test_doc_integration.py::test_scan_deletes_frozen_sessions_older_than_ttl`,
`test_hub.py::test_retention_delete_does_not_orphan_racing_thaw`,
`test_store.py::test_delete_session`.

**Residual — external sweeps.** The live-id guard depends on in-process hub
state. An external cron that directly calls `list_frozen_older_than` and
`delete_session` can still delete a thawed-but-not-yet-checkpointed live session
unless it coordinates with the running seance process.

### 2.7 Ticket single-use properties

`/v1/ticket` mints a Fernet-signed, short-lived, single-use member-auth token
(`identity.py` `mint_ticket` / `redeem_ticket`):

- **Minting is gated to gs-trusted hosts.** The endpoint checks the **raw socket
  peer** against `gs_trusted_nets` — never a forwarding header — and reads
  `X-GS-User-Id` / `X-GS-Username` from the trusted request
  (`httpapi.py` `ticket`, `_peer_trusted`). An empty `SEANCE_GS_TRUSTED_NETS`
  disables the endpoint entirely (403). Proof:
  `test_httpapi.py::test_ticket_trusted_peer_mints_redeemable_ticket`,
  `::test_ticket_untrusted_peer_forbidden`, `::test_ticket_empty_trusted_nets_disabled`.
- **Single-use.** Each ticket carries a random `jti`; redemption records the
  `jti` in a seen-set and a second redemption raises
  `AuthError("ticket already redeemed")`. Proof:
  `test_identity.py::test_ticket_single_use`.
- **Time-bounded.** TTL is `ticket_ttl` (default 120 s); an expired ticket is
  rejected (`::test_ticket_expired`). The seen-set is pruned of expired `jti`s
  and capped at 10 000 entries to bound memory. Unexpired entries are never
  evicted: new redemptions fail closed at capacity until expired entries can be
  pruned. Records remain through the ticket's final valid timestamp second
  (`::test_ticket_jti_cache_prunes_expired`,
  `::test_ticket_jti_cache_preserves_replay_protection_when_full`,
  `::test_ticket_replay_stays_blocked_through_the_final_valid_second`).
- End-to-end member identity: `test_integration_scenarios.py::test_g_ticket_flow_yields_member_identity`.

**Residual — single-process assumption.** The `jti` seen-set is in-process.
Seance runs as a **single process** today, so single-use holds globally. A future
multi-process/multi-host deployment would need a shared
seen-set (e.g. in the store) to preserve strict single-use; until then the 120 s
TTL bounds any replay window to one use *per process*.
