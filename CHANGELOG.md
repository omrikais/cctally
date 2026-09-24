# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.111.0] - 2026-09-24

### Fixed
- After `cctally update` or a Homebrew upgrade installs a newer version, a running `dashboard` restarts itself so new prices and code apply. Restart it yourself once after installing this release, and after any manual `npm install -g`.
- A `dashboard` restart after an update no longer leaves an extra Node process running on npm installs.

## [1.110.0] - 2026-09-22

### Added
- Claude cost reports price `claude-opus-5-5` at its published input, output, cache and Fast-mode rates instead of showing $0 for new usage.
- Codex cost reports price `gpt-6-luna` at its published Standard, long-context and Fast-mode rates, and resolve `gpt-daybreak-red-latest` to the current Cyber card.
- Historical `gpt-5.5-cyber` Codex turns use their retained LiteLLM OpenAI-provider rate card instead of the legacy `gpt-5` fallback.
- Codex reporting prices `gpt-6-sol` from OpenAI's published Standard, cached-input, long-context and Fast-mode rates instead of the legacy `gpt-5` fallback.
- The dashboard's `Blocks` panel marks Claude rows whose cost and model figures come from the facts retained when the block closed, without recalculating those figures from the current cache.
- Maintainers can turn latest full-backlog triage into a dependency-safe Codex execution plan and run automatically in two Sol/High worktree slots, with refresh-safe splitting, one resumable monitor, and no worker-created orchestration chain.

### Fixed
- Codex long-context requests above 272K input tokens now use OpenAI's higher input, cached-input and output rates for the entire request, including Fast-mode costs and dashboard cache savings.
- The dashboard promptly releases a Conversation Viewer live-tail thread when its reader closes, instead of retaining each one until the next 15-second keep-alive.
- The dashboard's Current Week modal reliably returns keyboard focus to the hero after Escape, even when a source-selector click is still pending under load.
- The `Blocks` detail modal connects its retained-facts explanation to the cost and token figures for screen readers.
- The `dashboard` fully hides its onboarding hint while an alert toast is active on a phone, so the alert action stays visible while the original onboarding timer continues.
- `cctally db rederive` retains a weekly cost snapshot when replay names the same range instants with different UTC-offset spelling, so reviewed corrections do not rewrite unchanged cost facts.
- `cctally db rederive` can apply an operator-reviewed weekly correction without carrying unrelated baseline drift. Version 2 manifests can preserve or release an accepted snapshot identity and pin both plan hashes before append.
- `cctally db rederive` preserves closed five-hour costs and accepted snapshot references when current replay rules revisit the same retained evidence.
- A Conversation Viewer link that waits for its outline now limits a failed page load to three requests, then shows the connection error with a Retry button for the linked message.
- `cctally db rederive` now refuses a correction that would add a burst of matching weekly reset events, and its JSON preview breaks action counts out by event kind for inspection.
- Dashboard diagnosis and conversation outlines now bound request surges and stalled reads. A timed-out diagnosis process is retired so later requests recover, and unchanged dashboard files revalidate without rereading or recompression.
- The dashboard's `Current Week` five-hour block now follows the account that supplied the displayed weekly percentage. Its credits and crossings stay with that account when two accounts share a window, including while browsing older blocks.
- The dashboard's Claude Session detail now shows cache rebuilds and cache savings over plain HTTP LAN access, including the healthy zero-rebuild state. Opening the same session over loopback and LAN shows the same verified outline.
- Dashboard panel headings now shorten before their action buttons escape narrow cards, including when Projects data is unavailable.
- The dashboard calls Claude's credited window a cycle in the hero and milestone modal. Its cost comparison names a comparable cycle, multi-account spend no longer implies one common cycle, and week-scoped panels keep their week labels.
- The dashboard's phone-width account cards now show a swipe cue while more cards remain, and long snapshot ages stay readable inside the hero.
- At phone widths where the dashboard source-status chip is shown, warning labels now name both the affected area and its state, such as `Accounts unavailable` or `Projects partial`.
- The dashboard's `Send test alert` preview now shows the same account chip and dismiss hint as a real alert on single-account and multi-account installs.
- Closing a dashboard modal now returns keyboard focus to its opener, and explanatory card text no longer opens a modal when clicked.
- The dashboard's All-source Daily detail shows a single-provider day's model costs once, without an empty second provider row.
- The dashboard's Projects modal presents its cycle window and chart scale as separate radio groups for screen readers.
- Corrupt Codex token or timestamp values no longer empty the provider. Valid costs and projects remain visible while affected rows are skipped and counted, including rows in a budget period older than the detail view.
- Long identifiers in Doctor remediation text now wrap inside the modal at narrow screen widths instead of being clipped.
- One Codex conversation whose stored project path cannot be read no longer empties the whole Codex provider. Every other project is published from current data, and the affected rows are reported as withheld.
- A Codex project path the store cannot read is counted and named in `cctally doctor`, under a third reason beside the missing key and missing thread join it already reports.
- A refresh that could not read project metadata is replaced on the next refresh instead of being kept for up to two minutes.
- A refresh whose project metadata could not be read states so in the Projects panel body, so an empty panel is no longer silent about why.
- The Conversation Viewer opens a conversation whose stored project path cannot be read, showing no project instead of failing, and its spawned-agent links, raw export and live tail work again.
- No conversation list, filter, search hit or outline shows a project for a conversation whose stored project path can no longer be read.
- An anonymized transcript export refuses while any Codex project path cannot be read, rather than producing bytes that only look scrubbed. `cctally transcript export` exits 3 and the viewer says so; `--raw` is unaffected.
- An account-scoped anonymized Codex transcript now scrubs project paths tied to that account. If path ownership is uncertain, export and the viewer's Anon copy refuse instead of claiming the copy is anonymous.
- In the Conversation Viewer, a refused anonymized per-turn copy now shows a red X on the control, so a tap visibly reports that nothing was copied.
- The dashboard's Session detail no longer logs a failed outline request when a transcript was not kept. The Conversation Viewer names a temporarily unavailable transcript store on deep links and while search results load.
- `cctally explain` and the dashboard's Explain view retry a brief database change before reporting that the read could not complete.

## [1.109.0] - 2026-09-15

### Added
- The dashboard picks up a new pricing revision without a restart. It re-reads the deployed pricing file every 60 seconds, adopts a complete newer revision, and keeps serving; no connected client is dropped.
- The conversation filter popover says `Projects appear once indexing finishes.` in place of `No projects.` while the transcript index is being rebuilt. Model counts stay live on the Claude tab.
- The dashboard's Projects panel gives every billing cycle of a credited week its own percentage. Both cycles shared one calendar date, and only the later reading survived, so the second cycle could never carry one.
- The milestone modal names an observation gap where it showed a bare em dash, and states each back-filled run once above the table, as `cctally percent-breakdown` already did. A gap in the current cycle is disclosed too.

### Changed
- The dashboard's conversation routes open the transcript store read-only. Every browse used to run two write-capable pragmas first, so a read competed with maintenance for the SQLite write lock and lost.
- A conversation route answers with a typed degraded envelope instead of HTTP 500 while a rebuild, a reclaim pass or a schema upgrade holds the store. Sessions reappear on their own; no request returns a server error.
- Transcript reclaim runs under a two-second budget and continues on the next pass, so it no longer holds the maintenance and both provider locks until the freelist drains. It is reported as its own timed phase.
- `cctally project` reports the same per-cycle percentages as `cctally weekly` on the same store. A credited week now contributes one percentage per cycle to `totals.usedPercent`, so a week credited once reports both figures instead of one.
- A cycle with no observation of its own is reported as missing rather than given the previous cycle's reading, so a week credited `n` times can report up to `n + 1` independently missing observations.
- An observation captured at the exact instant of a weekly credit belongs to the cycle that starts there, matching where the spend recorded at that instant already goes.
- The Trend panel, the Trend modal, the Projects modal and the period modal count in billing cycles for both providers, and share artifacts follow.
- The Projects window pills render bare numbers under a `cycles` label, the Projects grid columns drop the `wk` prefix, and the period modal's `Subscription window` field is now `Reset cycle`.
- Two cycles of a credited week that fall on one calendar day now render distinct labels, with a time suffix on each. An uncredited week and an ordinarily credited week are unchanged.
- The dashboard's Projects share artifact states the period its data actually covers. It multiplied a cycle count by seven days, which overstates the span once a credited week is inside it.
- An All-tab project drill widens to a window that reaches the ranking the row came from, instead of assuming every cycle spans seven days.
- `cctally project`'s `Used %` cell counts billing cycles: `(3wk)` now reads `(3cy)`, and the trend share artifacts' `Δ` rows follow. A credited week contributes two cycles, so the week abbreviation named the wrong unit.
- Screen readers announce the Weekly modal's cost chart and the Current Week navigator in billing cycles on the Claude tab. They announced "week" and "period" while every visible label beside them said cycle.
- `--no-sync` no longer runs the two transcript derivations at startup, so a frozen dashboard starts promptly. While either is owed, conversation reads report that state rather than an empty list, until you run `cctally cache-sync`.
- A Codex project or quota block reports itself partial when any accounting record inside the past year is unreadable, and states that project metadata is withheld for that item, so a cost is no longer missing with nothing saying so.
- A momentary database error marks that refresh's Codex project and quota block views partial, and the next refresh recovers on its own once the store is healthy.
- When the dashboard cannot read the account registry, the Codex source says so: it reports the refresh as partial and states that per-account detail is withheld, instead of presenting the install as if it held one account.
- The source status chip names the withheld account detail instead of reading `Source degraded`, so a registry the dashboard could not read is distinguishable from a source that could not be built at all.
- The first journal rebuild after upgrading re-reads every segment once, because the stored segment-summary format changed. Later rebuilds skip unchanged segments as before.

### Fixed
- `cctally record-credit` plans a credit from the confirmed account's own snapshots, floors and five-hour readings, and its preview names the account it will write to. An install with one account is unchanged.
- `cctally record-usage`, `cctally report` and `cctally percent-breakdown` read each account's own week boundary, so one account's earliest capture no longer sets another account's week.
- `cctally explain` decides Codex prompt evidence per conversation. A conversation whose transcript is no longer retained leaves the evaluated population and lowers reported coverage, instead of counting as a confident non-contributor.
- A journal segment summary survives an ordinary volume remount, after proving the file's bytes are unchanged, so a rebuild no longer re-reads the whole journal once an external or network volume is remounted.
- The dashboard rebuilds a Codex view whose project metadata was incomplete instead of serving it again for the life of the process, and retries on a bounded schedule rather than once.
- The dashboard tells a temporary read failure apart from a record it cannot interpret, so only the second advises `cctally cache-sync --source codex --rebuild`.
- The Projects drill issues one request at the width where two panels are mounted, so resizing the window no longer fetches the same project twice.
- `cctally five-hour-breakdown` and the current-usage modal report the weekly percentage a reader saw at each five-hour crossing, not the raw reading a held tick recorded. Where that reading is no longer retained the column reads `—`.
- A five-hour block shows only its own account's crossings. One window owns one block per account, and both the current-usage modal and the historical cycle view listed every account's crossings under each.
- `cctally five-hour-blocks` and `cctally five-hour-breakdown` withhold a weekly percentage a weekly credit retired, at a block's start and end, closed and reset-crossing blocks included. A block observed only before the credit is unchanged.
- A held status-line tick keeps its five-hour reading through an in-place weekly credit. The credit's cleanup of stale pre-credit replays removed that tick's row, the only record of the reading it carried.
- A status-line tick whose weekly percentage is held at its high-water mark no longer discards that tick's five-hour usage, so `cctally five-hour-blocks` and the dashboard's five-hour panel stay current.
- A status-line tick held at the weekly high-water mark does not advance the weekly percentage or its freshness stamp. `cctally forecast`, `cctally report`, the status line, the TUI and the dashboard keep the last observed reading.
- The Codex source recovers on the next refresh once the account registry is readable again. A refresh that could not read it republished that result for the life of the process, so the per-account panels stayed missing until a restart.
- A Codex quota block whose retained metadata is incomplete opens and states that, instead of replacing the whole panel with a message saying the dashboard had updated. Nothing had updated.
- Above one Codex account, clicking a quota block row in the Blocks panel opens that row's own account while the account selector is on All accounts. One of two rows sharing a window opened the other account's block.
- Opening a Codex project or quota block in the dashboard is immediate. Each click read a year of Codex accounting first, which on a large store is every row it has, and now reads only that project's or that window's own rows.
- A Codex quota block opens while one unreadable Codex accounting record sits elsewhere in the store. The view converted a year of records it never used, so a single bad record failed the whole request.
- A Codex quota block renders every observation of its own window. The detail read a thousand recent rows, so a busy window could lose members, or arrive empty and report the block as missing.
- Above one Codex account, opening a quota block returns that account's block. Two accounts whose blocks share a root, a limit, a slot, a window length and a reset produced one link, and whichever sorted first was shown.
- A Codex quota block older than the two hundred and fiftieth most recent now opens instead of reporting itself missing.
- A Claude transcript keeps its account after its volume is remounted. A remount renumbers the device each transcript records, and the next time one grew, its whole history took the account signed in then. Existing damage is not repaired.
- The conversation list names the transcript-import state and the command that clears it, instead of reporting a load failure beside a Retry that could never succeed. The server reported that state; the page discarded it.
- The current-usage modal describes a reconciling Codex quota projection the way the hero does, rather than calling it unavailable directly above the figures it publishes. The Codex-only view carries that qualification too.
- A Codex hero that is showing its last coherent figure says so in text and to assistive technology. The explanation was in a tooltip, which a touch user cannot reach.
- Opening a Codex row in Recent Sessions is immediate. The detail rebuilt every Codex session of the past year on each click, and now uses the row the dashboard already published, so it reads no accounting data at all.
- The dashboard's Codex hero keeps its last known spend, with a quiet updating marker, while the quota projection reconciles. It blanked the figure for that moment, and the account card beneath it went on showing a number.
- A Codex hero says it is pending, rather than rendering an empty value that reads as no spend, both before its first coherent reading and whenever its weekly cycle stops resolving afterwards.
- The dashboard publishes a recorded metering-rate change on the next refresh. Nothing the dashboard compared between refreshes changed when one was written, so an idle dashboard kept serving the alert list from before it.
- The dashboard evicts its least recently used Codex caches at their retained-memory budget instead of discarding all of them. A store permanently above the budget rebuilt every cached view on every refresh. Published figures are unchanged.
- The visible-population memo is still evicted whole, not in part, because it is one indivisible population. Two other cases take every Codex cache cold: a total eviction cannot reduce, and a refresh whose generation moved mid-build.
- The dashboard no longer discards its most expensive Codex caches first. Two reported no age and no size to the eviction order, so the visible-population memo and the quota memo always went first, however recently they had been used.
- `cctally dashboard-perf` reports what the Codex source caches retain. The figure came from a background measurement that never finished on a large store, so it read as zero.
- Codex spend and transcripts reach the dashboard within one tick of a rollout being appended to. An append made mid-turn left no evidence any freshness check examined, so only the two-minute safety walk ever found it.
- A Codex rollout cctally has read since this release is re-read from the start when it is replaced in place, even at an unchanged size and modification time. An older read position carries no file identity to compare.
- Adding or removing a `$CODEX_HOME` entry is noticed immediately. A configured root could be added or removed with nothing on disk changing, and the dashboard would keep reporting itself up to date.
- The conversation viewer keeps every session title and the browse list's cost, project and date columns while `cctally cache-sync --rebuild` replays. The rebuild cleared those tables first, so the list showed no titles throughout.
- A transcript sync keeps reading a file that the running session appended to mid-read. Any size or modification-time change read as a replacement, so an ordinary append discarded that pass's records and left the whole rebuild to run again.
- A transcript rebuild drops session titles whose sessions no longer exist. Every rebuild kept the previous titles in full, so titles for deleted sessions stayed forever and title search returned them.
- A transcript rebuild keeps each message's recorded account instead of re-attributing it to whichever account is active. The attribution was held in memory, so a rebuild resumed in a fresh process lost it.
- A transcript file replaced in place is recognised as a new file even when its size and modification time are unchanged, so records that no longer exist cannot lend their identity to whatever replaced them.
- `cctally cache-sync --rebuild` refuses, before deleting anything, when the volume holding `conversations.db` is nearly full, instead of failing partway through and leaving the store half-rebuilt.
- `cctally doctor` warns when it cannot read a store's rollup pricing fingerprint, instead of reporting no refused write. A locked store read as one that recorded nothing, and every writer of conversation cost authorized itself over it.
- `cctally cache-sync --prune-orphans` exits 3 and names the affected files when the conversation rollup re-derive is refused. It reported success, and the dashboard's automatic prune said nothing at all.
- The conversation browse rail falls back to live aggregation when the rollup's state cannot be read, instead of reporting it authoritative over a failed read. Sessions stay visible; cost sorting and project filtering degrade.
- `cctally doctor` reports a transcript reclaim backlog that is not draining, and fails above 16 GiB. A rebuild is refused at that point, because it adds churn to a store already failing to return the space it freed.
- `cctally cache-sync --rebuild` measures the free space it needs against the store instead of a fixed 64 MiB. A rebuild replays the whole corpus before the pages it frees come back, so the fixed figure admitted rebuilds that ran out of room.
- A transcript rebuild that finds no files on disk no longer deletes every message's recorded account. An unmounted volume produced that walk, and the account is the one thing in the store that cannot be derived again.
- A `cctally dashboard --no-sync` run advances the transcript schema once at startup. Nothing else does in that mode, so a store one migration behind stayed behind and every conversation route stayed degraded.
- `cctally five-hour-blocks`, `five-hour-breakdown` and the dashboard's Blocks panel record a five-hour credit only when one reporting source both observes the drop and confirms it. A stale status-line sample below an API reading minted one.
- `cctally statusline` records a weekly reset within 180 seconds even while another session still reports the pre-reset percentage. Agreement from every session was required, so one session that never agreed held the reset back forever.
- `cctally record-credit` keeps its journalled cleanup when the operation is retried. The list of rows to remove followed database row order, so a retry could describe the same removal differently and have the second description withheld.
- The stats-database writer guard `cctally doctor` reports now checks every write, not only the first of its kind on a connection. A repeated write that reused an earlier statement went unchecked, so it was neither refused nor recorded.
- `cctally five-hour-blocks` totals, their breakdowns and the dashboard's Blocks panel count an entry once when a reset shift makes two windows overlap. The later window also priced the earlier one's overlapping entries.
- The dashboard's Blocks panel and its block modal report a closed five-hour block's recorded totals and model split. They recomputed from the cache, so a session ingested after the block closed changed numbers that are meant to be final.
- The Blocks share artifact counts a project's cost in one five-hour block only. It read the recorded rollup with a key that never matched, then swept each block's raw interval, so two overlapping windows both claimed the shared entries.
- The dashboard's block modal says when a closed five-hour block's headline comes from the record kept at its close while the chart below is drawn from the local cache. The two can differ, and nothing said so.
- After `cctally cache-sync --rebuild`, `cctally doctor` warns that `conversations.db` has space to reclaim until the passes drain it. The dashboard drains it; without one, run `cctally db vacuum --db conversations`.
- `cctally cache-sync --rebuild` still runs when the free space on its volume cannot be measured. A failed measurement is now unknown and the check is skipped; it used to read as too little space and defer every rebuild.
- The conversation viewer's live-tail stream answers immediately while a rebuild holds the transcript store, instead of holding the connection open for the whole rebuild. Two other conversation-route paths answer the same way.
- A dashboard request, panel rebuild or transcript sync that is under way when a new pricing revision is adopted finishes on the revision it started with, so one response is never priced from two revisions.
- `cctally cache-report`'s per-call tier threshold follows an adopted pricing revision. It was a fixed figure that a reload could not replace.
- A transcript schema upgrade is started once when several conversation requests find the store behind head at the same moment, instead of once per request.
- The dashboard's Codex cache report keeps each row on the day it belongs to when a row moves to another day. Its former day kept a copy, so the report either refused to build or published the row under a date it had left.

### Maintenance
- The test-evidence scrub no longer redacts a diagnostic line for containing the ordinary word "token". A token credential now needs a real separator, so a line such as `unexpected token ')' at line 4` is published rather than replaced.
- A failing envelope-oracle verification keeps the envelope that disagreed with the baseline, and the sanitized failure extract names it, so a mismatch can be diagnosed from the retained evidence instead of by reproducing it.
- The sanitized failure extract is withheld whole, and says which check withheld it, when the scrub's transformer fails a health probe before publication. A per-line refusal could otherwise publish beside a secret the backstop cannot see.
- The envelope-oracle harness emits its results in the one form the failure-extract reader parses, bringing it under the harness scrub contract so its output survives sanitization.

## [1.108.0] - 2026-09-05

### Added
- Codex reporting prices `gpt-6-astra` from OpenAI's published standard, cached-input, long-context and Fast-mode rates instead of the legacy `gpt-5` fallback.
- `cctally alerts test` gains `--axis quota` and `--axis meter-rate-change`, so you can rehearse those two notifications instead of waiting for a real crossing or rate change. `--threshold` does not apply to `meter-rate-change`.
- `cctally percent-breakdown` and `cctally report --detail` name a run of percent thresholds that one observation recorded, and print `observation_gap` where the marginal cost showed a bare `n/a`. `--json` adds `marginalCostWithheldCause`.
- On an install with more than one account for a provider, every alert toast names the account it belongs to, as the dashboard's Recent Alerts list already did.
- `cctally doctor` gains `hooks.codex_liveness_7d`, which FAILs when an enabled Codex hook has not succeeded in seven days. It reads the per-root success markers, so log rotation cannot hide a hook that stopped firing.

### Changed
- `cctally weekly`, `cctally report` and `cctally project` render a week credited twice as three rows, one per billing cycle. A dictionary keyed on the week alone now keeps only the last cycle; key on the week and its start instant together.
- `cctally weekly`, `cctally report` and `cctally project` start the first cycle of a week whose boundary moved and was then credited at the moved boundary. The previous week keeps its own spend, which was counted into the credited week.
- Upgrading from 1.107.0 or earlier rebuilds the disposable stats index once, because a weekly reset now records the observation it came from and a rate change records its evidence. Commands report the rebuild until it finishes.
- A metering-rate change recorded before this upgrade reports no evidence rather than inventing any.
- A weekly credit is recorded at the exact second Anthropic issued it rather than the top of that hour, so a reading captured earlier in the same hour stays in the cycle it belongs to.
- A pre-credit reading captured before a second weekly credit now survives and is shown inside the earlier cycle, because each credit's cleanup is scoped to its own instant instead of reaching back past the previous credit.
- `cctally doctor` now reads Codex's own hook trust record and FAILs when the cctally handler is disabled or was never trusted in Codex `/hooks`, instead of reporting it as installed.
- `cctally doctor` warns when the Codex handler changed after Codex last recorded a trust decision about it, and when that record cannot be read at all.
- `cctally setup` refuses at exit 1 when reconciling the Codex handler would land it on a trust decision Codex recorded about a different handler. The message names the file, the slots and the fix.
- When Codex's trust record changes mid-run, the `cctally setup` refusal names each Codex hooks file it had already rewritten and the dated backup of its previous contents. `--json` adds `changed_hooks_backups`.
- `cctally doctor` states how many Codex roots are installed beside how many are enabled, so two installed-but-untrusted handlers no longer read as `0/2 root(s) enabled` alone.
- `cctally setup` recognizes a Codex handler written by a different install channel and collapses it into one canonical handler, instead of adding a second one beside it.
- `cctally setup --json` moves to `schema_version: 2`: the `installed_review_required` state is retired, and per-root `changes` becomes a per-event object with separate added, removed and unchanged counts.
- `cctally setup --dry-run` and the applied run now report the same added, removed and unchanged Codex handler counts for the same input.
- The dashboard and the TUI stop treating a Codex handler as trusted merely because it is present, and re-check that trust when `config.toml` changes, so a hook disabled while either was running no longer certifies stale conversation cost.
- A freshly installed Codex handler reports as untrusted, and `cctally doctor` FAILs, until you approve it in Codex `/hooks`. Until then the dashboard and the TUI walk every Codex session on each refresh.
- Two `cctally setup` messages read as ordinary English again: the orphaned `[hooks.state]` remedy says to remove the entry from the file by hand, and a moved trust slot says the handler would move from one key to the other.

### Fixed
- A percent milestone crossed on a week that holds two recorded resets is listed again. It was filed against one cycle while `cctally percent-breakdown` listed another, so the crossing appeared under neither.
- Every view of a credited week picks the cycle the week is currently in, not the reset written last. `cctally diff`, the TUI's per-percent modal and the dashboard's milestone list could each name an earlier cycle.
- A weekly threshold notification names the billing cycle that crossed. On a week credited more than once it read `Week starting Jun 05` for every cycle, naming all of them at once; an uncredited week reads exactly as before.
- A second Anthropic usage reset inside one subscription week now registers. Every reset in a week shared one slot, so the first held it and later ones were discarded, leaving the reported percentage at the pre-reset high-water mark.
- A weekly reset observed while cctally is interrupted is no longer lost, and one zero reading can no longer confirm itself into a credit. The pending-reset state moved into the database and commits or rolls back with the reset it fired.
- A stale pre-credit reading that a credit removed stays removed after `cctally db rebuild --db stats`. The removal is recorded, so a rebuild no longer restores the reading and holds the reported percentage at the pre-credit high.
- An idle session no longer holds the reported weekly percentage at a stale value. It re-renders its status line from a cached rate-limit block, and that block is now ignored in full once its five-hour window has closed.
- A severe metering-rate drop now raises a red alert toast instead of the amber that made it look like a smaller drop, and the toast's rate chip matches the colour Recent Alerts already showed for the same event.
- A metering-rate change detected while the quota calibration was withheld now records which calibration was withheld and how many baseline days were missing, instead of pointing you at a fitted budget that may never have been produced.
- A metering-rate change whose notification failed and is retried later now carries that same disclosure, because it is read back from the recorded change rather than re-derived from a later run.
- A metering-rate toast now names the calibration that was withheld and why, instead of pointing you at a fitted budget that may not exist. An ordinary rate change reads exactly as it did before.
- Recent Alerts shows the evidence behind a metering-rate change: the withheld calibration, the detector's inputs, the composition provenance, and how many baseline days were withheld.
- An alert toast on a multi-account install states how to dismiss it again, on its own line below the alert's own text rather than above it, so a screen reader reads the alert before it reads how to close it.
- Alert toasts are usable from the keyboard: press Enter or Space to dismiss one, or to activate its own button and open the window it names. A focused toast states the alert before how to close it, and dismissing one restores focus.
- The dashboard's Daily panel reads `loading` while it hydrates, instead of reporting the 30-day window as `withheld` over a loading skeleton.
- The dashboard's Trend panel states its title in full on a phone. The week and cycle counts moved to the wrapping sub-line the other cards use, so the title is no longer cut off mid-word.
- The dashboard's Daily panel prints its total and peak-day amounts in full on a narrow window. Below about 400px the two summary columns stack instead of cutting the dollar figures off.
- An install upgrading from a version that predates the append-only journal no longer records a second copy of a reset it already had, so a credited week keeps its own cycles instead of gaining an extra one.

## [1.107.0] - 2026-09-04

### Changed
- The same-window weekly credit model introduced in 1.106.0 is reverted. A week is anchored to its billing cycle again, and a recorded credit no longer rewrites the week in place or withholds the `$/1%` figure.
- Upgrading from 1.106.0 rebuilds the disposable stats index once. On a large store that took about a minute here, and commands report that a rebuild is running until it finishes.
- `claude-fable-5-1` and `claude-mythos-5-1` each count as their own model family in the weekly quota model, instead of being pooled with the model they follow. Your quota calibration refits once on the next `cctally quota` run.
- `cctally doctor` separates the one refused-pricing-write state that restarting cctally cannot clear — a recorded pricing date it cannot read — from the ordinary one, and names the step that does clear it.
- A shared `cctally weekly` artifact labels each row by the date its cycle began, matching the terminal table and the dashboard. On a week Anthropic reset early, that label and the artifact's stated period both move forward.
- The dashboard's Projects panel counts a credited week as the two billing cycles it is, so its week grid covers fewer calendar days for the same week count.
- The `cctally weekly` and `cctally report` artifacts, the dashboard's Weekly panel footer and the Weekly modal title all count billing cycles rather than weeks, because a credited week supplies two rows. `Avg %/wk` is now `Avg %/cycle`.

### Fixed
- `cctally weekly` renders a week credited in place as two rows, one per billing cycle, matching `cctally report`. The cycle before the credit had no row at all, and its spend was missing from the table and from `--json` totals.
- The dashboard's Weekly panel and the TUI show both cycles of a credited week from one source, so a week's two rows can no longer disagree with the panel total beside them.
- `cctally project` counts a credited week as the two billing cycles it is, so `--weeks N` spans a shorter calendar range there. `attributedUsedPercent` rises, because the cycle before the credit matches its usage snapshot again.
- A `cctally dashboard` left running across a pricing update can no longer overwrite corrected conversation cost from the table it started with. It keeps ingesting, your conversations stay listed, and `cctally doctor` says to restart it.
- `cctally doctor` reports a refused conversation-cost write from any process that hit it, including short-lived ones like the status line and `cctally daily` that never open the conversation store.
- `cctally cache-sync --rebuild` says it did nothing when it may not rewrite conversation cost, instead of reporting `0 processed` and exiting 0. It names both reasons: a stored pricing table newer than this one, or one that is not a date.
- `cctally doctor`'s refused-pricing-write warning clears once cctally is running current with the store, including after a refused `cctally cache-sync --rebuild`. That refusal previously left a warning the documented remedy could not clear.

## [1.106.0] - 2026-09-03

### Changed
- `cctally record-credit --force` replaces the single credit `--at` names instead of clearing the whole week, and refuses when `--at` names none. Recording a second credit in one week needs no `--force`: rerun with a different `--at`.
- A week Anthropic credited in place renders as one row on its own boundaries, instead of two rows split at the credit. A credit that also moved the week's declared end is marked, but still renders on the windows the API stated.
- The dashboard's Current Week header and modal name the week's own boundaries on a credited week, instead of the window that starts at the credit.
- A credited week's row is marked `+` in `cctally report` and `cctally weekly`, so a low `Used %` beside a week's worth of spend has a visible explanation.
- The dashboard's Weekly panel and its week detail mark a credited week, and the week detail, the Current Week card and its modal name why `$/1%` is withheld instead of the em-dash or `$0.000` you cannot tell from a week with no usage.
- The dashboard's `$/1%` tables — the Weekly modal's week list, the `$/1% Trend` panel and its modal — say `No climb` for a credited week whose ratio is withheld, instead of the em-dash you cannot tell from a week with no usage.
- `$/1%` for a credited week is measured from the credit forward, over the climb since it. With no climb yet the figure is withheld and names why. Uncredited weeks are unchanged.
- The dashboard's Current Week card, the TUI header and a shared Current Week recap report a credited week's whole spend, matching the week they name and the Weekly card beside them. Only `$/1%` is measured from the credit forward.
- `cctally milestone` history lists a credited week once instead of once per credit, and its detail separates the week's percent ladders with a row naming each credit and the level it dropped from.

### Fixed
- A conversation open in the viewer updates its own cost as its turns stream, for both Claude and Codex. The live tail advanced the transcript but not the accounting the cost is read from.
- A Codex conversation's cost no longer reads `$0.00` long after its turns finish. The dashboard now re-scans each provider at least every two minutes, so accounting stays current even when that provider's activity hooks never fire.
- Upgrading an existing installation to v1.105.0 left every background sync failing and the dashboard stuck on `server sync error`. The two columns that release added now reach an existing database, not only a freshly created one.
- After Anthropic zeroes your weekly counter, every 7d surface lowers on the next status-line tick. A reading captured in the same hour no longer holds the week at its pre-credit percentage, and no manual command is needed.
- A goodwill credit to a non-zero level records that level's own percent milestone. Before, the ladder for the new period opened one percent late, and `cctally percent-breakdown` was missing its first row.
- `cctally percent-breakdown`, the TUI's milestone panel and the milestone writer agree on which credit a percent belongs to, including after a database rebuild and for a credit recorded with `cctally record-credit`.
- A percent milestone crossed after a credit reports the cost spent since that credit. After a credit recorded with `cctally record-credit` it reported the whole week's spend, which made the first post-credit `$/1%` figure far too high.
- `cctally record-credit` no longer deletes genuine usage recorded between the moment you assert with `--at` and the moment you run the command.
- On an install with more than one Claude account, a reset detected for one account no longer records a credit for another, and no longer discards the first account's pending detection.
- `cctally forecast` measures its rate from the latest credit rather than across it, including one you recorded yourself. `cctally budget` keeps summing the whole week, because the money was spent inside one unchanged window.
- `cctally doctor`'s post-credit milestone check now sees a week credited with `cctally record-credit`, which it silently skipped before.
- `cctally record-credit` accepts a second credit inside the same hour as the first. Only a credit at the identical instant is still refused, because that is indistinguishable from running the command twice.
- An install upgrading from a version that predates the append-only journal now gets the current weekly-credit table shape, instead of reporting the new database version while missing the columns a credit needs.
- `cctally record-credit --force` no longer leaves a replaced credit's percent milestones behind after a database rebuild, and `cctally record-usage` no longer leaves a removed stale reading's five-hour milestones pointing at it.
- `cctally` no longer records a second, automatically-shaped credit for a large credit you already recorded with `cctally record-credit`.
- A stale pre-credit reading replayed by the status line after a credit is removed on the next tick that contradicts it, instead of holding every 7d surface at the old percentage and provoking a second, phantom credit.
- On an install with more than one Claude account, `cctally record-credit` no longer names another account's credit when refusing, and no longer finishes another account's interrupted credit as though it were yours.

## [1.105.0] - 2026-09-02

### Added
- `cctally` prices Claude Fable 5.1 and Claude Mythos 5.1, including their cache reads at $0.25 per million tokens, so usage on either model is costed instead of contributing nothing with only a warning.

### Changed
- `cctally quota` counts Claude Fable 5.1 and Claude Mythos 5.1 as part of the Fable 5 and Mythos 5 families, so a week containing that usage no longer has its weekly-quota fit withheld as an unsupported model mix.
- `cctally quota` refits your stored calibration once after this upgrade, because the set of model names it recognises has changed.
- `cctally quota` excludes fast-mode requests that Anthropic bills to usage credits, and widens its family-composition support floor to the corrected empirical bound.
- The dashboard's Forecast basis and `cctally statusline` use one vocabulary; the status line remains a meter-only hot path, while Forecast can name its calibrated model.
- The dashboard's Conversation viewer opens long histories from a bounded, integrity-checked outline ticket, hydrates the exact account-scoped outline automatically, and skips unchanged transcript estates between updates.
- `cctally dashboard` compresses and caches content-hashed assets and loads conversations and detail views only when opened, cutting cold JavaScript transfer while warm reloads reuse unchanged code.

### Fixed
- `cctally dashboard` shares a progressive outline's first-chunk build, cancels abandoned transfers, and rejects an oversized outline before allocating its encoded body.
- `cctally dashboard` applies failed or over-limit background memory checks as soon as they finish, including while sync is paused, and its soak gate now distinguishes measured growth from noisy flat samples.
- `cctally dashboard` keeps production-size cache admission checks inside the whole-process memory ceiling without shrinking or evicting the retained caches.
- `cctally dashboard` stops loading a previous conversation's outline after you switch sessions, so the new reader stays current and avoids duplicate outline requests.
- Concurrent dashboard Explain requests share identical in-flight work and admit only one diagnosis process tree at a time, so multiple tabs or LAN clients no longer multiply provider workers and transient memory.
- `cctally dashboard` bounds retained source, snapshot, conversation, ingest, and SSE work; invalidates conversation caches after same-path database replacement; keeps the newest slow-tab update; and releases owners without slowing refreshes.
- `cctally quota` now records a metering-rate change it detected earlier but could not store at the time, so a busy or rebuilding database no longer costs you that entry in your rate-change history permanently.
- `cctally quota` delivers a metering-rate change notification that an interrupted write recorded but never announced, and remembers which ones it has announced, so upgrading never replays your whole rate-change history at once.
- `cctally quota` retries a metering-rate notification that was recorded without an account attribution, including on a run narrowed with `--account`, so an install that later adds a second Claude account no longer loses that announcement.
- A metering-rate change notification names the account whose rate changed, so two accounts changing rate no longer produce two identical popups. An install with one account, or none, sees exactly the notification it saw before.
- `cctally explain` and the dashboard's Explain modal build current, baseline and conversation-derived evidence within their documented warm and cold latency budgets without caching a stale report or slowing dashboard updates.
- `cctally doctor` reports `not assessed` rather than `no change detected` when no quota calibration has been fitted, so a withheld fit no longer reads as a confirmed all-clear about your metering rate.
- `cctally quota` records the durable metering-rate history whenever its detector confirms a change, even when the fitted budget is withheld because this week's composition sits outside the calibration's support.
- The metering-rate notification says the calibration was withheld, and names why, instead of promising a fitted budget it cannot supply.
- `cctally doctor` stops telling you to run `cctally quota` for a fitted budget when the calibration behind the change has none, and names the withholding instead.
- The dashboard's rate-change toast points at the evidence for a metering-rate change rather than promising a fitted budget.
- `cctally dashboard` skips both provider file estates on caught-up ticks, falls back to a full pass when hook activity cannot be recorded, and reports the main refresh loop's measured CPU duty.
- `cctally dashboard` reuses unchanged Codex accounting and account-card populations across quota and one-file updates, shortening active refreshes without changing cadence or freshness.
- `cctally quota` withholds malformed retained timestamps without a traceback, filters offset-bearing credit times by their instant, and emits every JSON timestamp with a trailing `Z`.
- Historical forecast replays ignore mid-week reset events until their recorded detection instant, so a future event can no longer erase the samples that were available at the replayed time.
- Weekly projected-pace alerts use the same model-backed value as `cctally forecast` when a quota calibration applies, instead of silently stopping for calibrated users.
- The dashboard's Forecast modal keeps an extreme projection label inside the card instead of clipping it at the 110% track edge.
- `cctally record-usage` no longer fabricates a percent milestone from a stale pre-credit reading after Anthropic zeroes your weekly counter, so a fresh week's milestone ladder no longer opens at a percentage you never reached.

## [1.104.0] - 2026-08-29

### Added
- `cctally quota` reports how much of your weekly quota your usage consumes and whether Anthropic has changed the metering rate, from a budget fitted to your own history rather than a shipped constant. See `docs/commands/quota.md`.
- `cctally quota --json` publishes the fit, the current week's consumption and projection, the composition support radii and the detector's own bound under `schemaVersion` 1.
- `cctally quota --reset-calibration` discards the stored calibration for one account, and `--since` and `--watch-from` narrow or override the window the analysis uses.
- `bin/cctally-preflight` compiles every Python file in the repository, not only the ones beside it, so a syntax error anywhere in the tree now blocks a test run instead of surfacing during one.
- `cctally forecast --json` states the reading its arithmetic used and where its projection came from, through `weekly_percent_corrected`, `right_censored`, `projection_basis`, `projection_code` and `calibration_code`.
- `cctally forecast --json` moves to `schemaVersion` 2: its three projection fields can now be null, and they are measured from a different reading than before.
- `cctally project --json` moves to `schemaVersion` 2. `attributedUsedPercent` and `costPerPercent` keep their spelling and change their meaning, from a share of the window's cost to modelled weekly quota.
- `cctally project` reports each project's `Used %` as modelled quota units rather than as its share of the window's dollars, so a cache-heavy project no longer reads high and an output-heavy Opus one no longer reads low.
- `cctally doctor` reports a new Quota category: whether Anthropic changed your weekly metering rate, and whether the stored calibration is usable. Neither check can fail, so neither changes doctor's exit code.
- `cctally quota` records a durable event when it confirms a metering-rate change, so it and `doctor` report the change with no configuration. The desktop notification stays off until you set `alerts.rate_change_enabled`.
- `cctally project` states its four modelled-quota figures under the table — window total, listed-row total, filtered or unmodelled, and the meter's reading minus the modelled total — where before they were in `--json` only.
- The status line's 7d slot shows where the week is heading and which measurement said so, as `7d 42% (2d 3h) → 58% meter`, plus a `Δrate` marker while a metering-rate change is in force.
- The dashboard's Forecast panel names the basis its projection came from and shows a `Δ rate` chip while a metering-rate change is in force.
- The dashboard's Forecast modal adds the modelled consumption, the headroom, what the meter reading covers, and the meter's reading minus the modelled total.
- The dashboard's Recent Alerts modal lists metering-rate changes in their own section, with the size and direction of the change and the instant it took effect, and a rate change now raises its own toast.
- `alerts.rate_change_enabled` is settable with `cctally config set` and from the dashboard's Settings overlay, so turning the notification on no longer means editing `config.json` by hand.
- `bin/cctally-test-all` refuses to start when the recorded test estate disagrees with what this tree collects, when a recorded test disappeared without a declaration authorizing it, or when it cannot derive the estate at all.
- `bin/cctally-test-all --with-estate-check` runs that check on a `--harness` subset, which otherwise skips it to keep a targeted run fast.

### Changed
- `cctally forecast`, the TUI and the dashboard measure your pace from what you consumed rather than from the rounded-up meter reading, so rates and projections read about half a point lower and daily budgets a little larger.
- At a displayed 100% the forecast shows no projection, no time-to-cap and no daily budget, because a capped meter says only that you are past 99%. `cctally quota` still models consumption there, from tokens.
- The projected-pace weekly alert stays quiet once `cctally quota` has fitted your budget, rather than firing on a pace the forecast no longer shows you.
- `cctally forecast`'s trailing four-week dollar rate divides each prior week's cost by the meter movement that week realized, not by its post-credit high-water mark, which priced one measured week at $355 per point against a true $25.
- A prior week whose realized movement cannot be established is left out of the forecast's four-week median rather than guessed at, and so is a week whose meter reached 100%.
- `cctally project` withholds its meter-versus-modelled difference when the requested range slices a subscription week, instead of stating a whole-week difference beside a three-day range.
- `cctally forecast` names its dollar rate's source in words — `trailing 4wk median (drift-reduced confidence)` rather than the bare code — and says when the comparability test behind that rate could not run.
- `rates.week_average_pct_per_hour` in `cctally forecast --json` is null at a reading of 100% or more, because a capped meter supplies no rate to average.
- `cctally quota` refuses to predict from a metering rate it has only just detected. It says so, and the forecast, the TUI and the dashboard fall back to the meter and state the reason, rather than projecting from a three-day fit.
- `bin/cctally-preflight` lists the repository once instead of once per check, so widening the Python check to the whole tree costs one pass rather than two.
- `docs/commands/report.md` states that `$ / 1%` falls when the provider charges more points for the same tokens, so a drop can read as improved efficiency, and points at `cctally quota` as the detector. The metric itself is unchanged.
- `cctally project` names the absent operand when it withholds the meter-versus-modelled difference — a modelled week with no snapshot, or a window that modelled no week — instead of reporting either as a misaligned population.
- `bin/cctally-test-all` builds both of its pytest phases from one recorded declaration rather than from hard-coded file names, and refuses at startup when a named target no longer exists.
- `bin/cctally-test-all` removes `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` from both pytest phases so neither can narrow a run; set `CCTALLY_PYTEST_DURATIONS=1` for per-test timings instead.

### Fixed
- The dashboard keeps the full account-count value visible in the `All` hero at tablet widths instead of clipping it mid-word.
- A shared forecast artifact writes a ceiling more than 30 days out as `>30d` instead of printing the literal figure, which on a barely-used week read `1166.8`.
- The terminal forecast's explain modal says why it shows no daily budgets at a capped meter, instead of printing the heading over nothing.
- `bin/cctally-preflight` bounds every command it runs, including the harness-ownership check, and reports a hung system bash version probe rather than silently skipping the bash 3.2 floor check.
- `bin/cctally-preflight` stops its shell syntax sweep and reports what went unchecked when an interpreter hangs, rather than spending its full per-file timeout on every remaining file.
- `cctally project` withholds modelled quota on a multi-account install whose account registry cannot be read, instead of falling back to a cost share under the `Used %` column.
- `cctally forecast` leaves a prior week out of its four-week median when the recorded credit or reset rows for that week cannot be read, instead of summing the week as if the credit had zeroed the meter.
- `cctally forecast` keeps prior weeks from before a metering-rate change out of its four-week median even when it cannot read the entry cache, instead of admitting every one of them after a single failed read.
- The dashboard hero shows no week-over-week `$ / 1%` comparison when it has no `$ / 1%` to compare, instead of printing a change beside a dash.
- The dashboard's Forecast panel keeps its pace bar visible and its content inside the card when the source reports a degradation, where the bar collapsed to nothing and the panel overflowed.
- The dashboard's Forecast modal says when a published `$ / 1%` rate was measured over a reduced or unverified population, where before the qualification was only visible in `--json`.
- A metering-rate change desktop notification shows the effective date in your own timezone rather than in UTC.
- Interrupting `cctally quota` with Ctrl-C while it records a metering-rate change stops the command, rather than printing a recording failure and carrying on.
- `cctally quota` carries on when it cannot read your stored calibration file, rather than stopping with a Python traceback.
- The status line's 7d slot shows no projection once the stored reset instant has passed, rather than projecting over a week that has already ended.
- The dashboard's Forecast panel puts its verdict, status and `Δ rate` chips on one line instead of stacking them, giving the card back a row of height. The Forecast modal's Quota model heading is now tinted like every other section heading.

## [1.103.0] - 2026-08-26

### Added
- Codex reporting prices `gpt-5.6-cyber` from its own rate card instead of falling back to `gpt-5`, so its turns are no longer marked as estimated.

### Changed
- The changelog you are reading is rewritten to be about what changed for you. Every entry is one plain line naming the command, flag or panel it touches, with no issue numbers and no files a reader cannot open.
- `gpt-5.6-sol` is priced at its standard rate rather than OpenAI's temporary promotional rate, so Sol figures read high by 25% on input and 50% on output while the promotion runs. The bare `gpt-5.6` name resolves to the same rate card.

### Fixed
- Codex per-account accounting keeps a continued session's history when the token counter restarts mid-file. Run `cctally cache-sync --source codex --rebuild` to restore the omitted turns.

## [1.102.0] - 2026-08-22

### Added
- `cctally explain` reports which models, projects, sessions, 5-hour bursts, prompt-cache churn, and subagent fan-out account for a window's spend, and names a command to run for each.

### Fixed
- The README's Latest stable section names the full upgrade range and links its release notes, instead of showing three highlights from one release.
- Dashboard modals return stray Tab focus to the topmost open modal, and the five-hour blocks, block detail and recent alerts views name their display time zone.
- The dashboard opens the current five-hour block again when Anthropic shifts a reset far enough that two block windows overlap. The detail view previously failed with HTTP 500.
- With more than one account, the dashboard header keeps every account chip and action on one row, and at 480px keeps the account selector and full-size tap targets without sideways scrolling.
- The dashboard's Projects panel builds faster, because it reads only the latest usage snapshot for each week instead of the whole snapshot history.
- Dashboard refresh state tells a queued rebuild apart from one already running, keeps the sync text and its screen-reader state in step, and writes an over-budget remainder as `-$1.23`.
- `cctally explain` evaluates production-sized Codex windows instead of spending its analysis budget on unrelated event rows.

## [1.101.0] - 2026-08-19

### Changed
- Dashboard refreshes release their read lock on `cache.db` sooner, cutting the measured median hold from 3.40 seconds to 1.04. The published data is unchanged.
- Every dashboard warning carries a button that opens the surface explaining it — the week, the five-hour block, the month, the project, or the forecast. A warning whose window has closed says so and opens nothing.
- The dashboard's Projects table writes out what its two percentages mean under the week selector, where a phone can read it. `Used pp` is relabelled `Used pp (sum)`, and the caption names the denominator of `Cost share`.
- The conversation rail states that the message count beside each session includes subagent sidechains, so a heavily delegated session no longer reads as a much longer conversation. The count itself is unchanged.
- Every alert notification ends with the command that explains it, scoped to that alert's own provider and window. Where the alert did not record enough to name a window, the line says so and offers no command.
- The warnings from `cctally forecast`, `budget`, `cache-report`, `project` and `diff` name the command that explains them, over the same window. Healthy output and every `--json` payload are unchanged.
- Open dashboard tabs share one update stream, so each server frame is parsed once and handed to every tab. A hidden tab still suspends on its own.
- Project drill-downs render the exact provider-native interval the server queried, instead of rebuilding Claude's interval in the browser.

### Fixed
- The dashboard Projects table and its drill-down measure the same subscription-week buckets at every `1w`, `4w`, `8w` and `12w` setting, so their cost and session totals reconcile. The drill states when its span contains reset gaps.
- Project attribution percentages describe the subscription week they are shown against, not an ISO Monday week. `attributed_pct` changes for every account whose reset is not Monday midnight UTC, historical weeks included.
- `cctally project --weeks N` measures exactly the N subscription weeks it names. It stepped back a fixed 7 × (N-1) days before, which pulled part of an extra week into the totals whenever a reset had drifted.
- `cctally project --account <ref>` buckets that account's spend against that account's own weekly boundaries and reports that account's own quota percentage. A run without `--account` is unchanged.
- Five-hour block totals no longer shrink where a block crosses a week boundary. The dashboard Blocks panel reports each block's whole native total, matching its own detail view.
- `cctally cache-report` gains an `Eval` column saying which of `anomaly`, `clear`, `partial` or `not eval` each row is in. At 120 columns the daily table drops `Input` to make room; `--json` is unchanged.
- `cctally forecast` reports the dollars-per-percent rate as unavailable rather than `$0.00` for a week with no observed usage. In `--json`, `dollars_per_percent` is `null` and `dollars_per_percent_source` reads `no_usage_observed`.
- Shareable weekly, `$ / 1%` trend and forecast artifacts print `n/a` instead of a measured-looking zero for a figure nothing measured. Artifacts whose figures were all measured are byte-for-byte unchanged.
- `cctally report --source all` renders the whole Claude report instead of the single line `Data available.` The `--source all` forms of `project`, `range-cost` and `cache-report` no longer print it either.
- The `cctally report` trend table marks a week shorter than seven days with `~` in the `#` column and explains the marker below. Ordinary reset jitter under an hour is never marked.
- `cctally budget` annotates a low-confidence projection `(LOW CONF — limited evidence)`. The old wording blamed an early week, which was untrue on a fully elapsed period with no spend.
- `cctally forecast` writes its low-confidence reasons in words rather than internal codes: `less than 24 hours into the week`, not `elapsed_hours<24`. `--json` still carries the codes.
- `cctally project` gains a `Cost Share` column giving each project's share of the listed projects' total cost, with that total and project count stated below the table. At 120 columns more headers abbreviate.
- The dashboard's forecast tile states when its data is stale, degraded, or limited by what a provider can report. A single-provider tab drew the projection and said nothing.
- The dashboard's forecast detail explains an unavailable `$ / 1%` instead of printing a bare dash that could not be told apart from a failure to load.
- The dashboard's `$/1%` sparkline leaves a gap for a week whose rate was never measured, instead of drawing a bar on the axis that reads as a collapse to nearly zero.
- A historical week whose exact start and reset were never recorded says so when you open it, instead of showing a bare date and a dash. Its milestones, percentage and progress bar were always correct.
- The dashboard's week-history list no longer empties when one historical week is missing its reset instant. That week is listed with its date label and no window, and every other week is listed as before.
- Codex reports recognize `gpt-daybreak-blue-latest` as the runtime alias of `gpt-5.6-sol`, so retained Daybreak usage is priced from the Sol rate cards with no unknown-model warning.
- The dashboard tells paused, resuming, disconnected and silent-stream states apart, with accurate wording and a wall-clock data age instead of generic connection advice.
- A multi-account `All` view shows combined spend only when every visible account subtotal and native provider period reconciles. Otherwise it explains why the figure is withheld.
- Doctor remediation commands keep each CLI flag intact at 320px instead of wrapping between the hyphens and the flag name.
- All seven dashboard range notes stay visible in less panel height, restoring room for Recent Alerts on desktop without creating phone overflow.

## [1.100.0] - 2026-08-18

### Added
- `cctally dashboard-perf` reports what a tick of the running dashboard costs, without restarting it. It states the publish period separately for Codex-active and Codex-idle ticks, and `--trace on|off` arms the deep phase trace.
- Clicking the dashboard's sync chip while a rebuild is running queues your refresh instead of dropping it with a `503`. The dashboard reports it back as queued, then running, then settled.
- `cctally doctor` reports the size of the transcript store's write-ahead log and warns above 256 MiB, and `cctally db checkpoint --db conversations` drains it.
- `CCTALLY_DASHBOARD_API_TOKEN` lets a `record-usage` nudge authenticate against a dashboard started with LAN access and a bearer token.

### Changed
- The dashboard sends far less data on each update and now negotiates gzip. One update over the project's own corpus fell from 305,466 bytes to 26,155. A client that does not ask for compression receives exactly the JSON it did before.
- If you read `sources.all.data.providers` from `/api/data`, read `sources.claude.data` and `sources.codex.data` instead. Both members of the old key are now `null`, and the wire version is 10.
- Loading the dashboard transfers the state once rather than twice, halving what a cold load downloads and parses.
- A dashboard tab you are not looking at stops costing anything: after thirty seconds hidden it disconnects its update stream and reconnects when you return. An alert that fires while it is hidden reaches the alerts panel but raises no toast.
- Connected tabs share one encoded copy of each update frame, so extra tabs no longer repeat the encoding of the same multi-megabyte frame.
- A Codex dashboard with more than one account no longer does extra database work per account on every rebuild, and one quota read is reused between rebuilds. Nothing published changes.
- The dashboard's health check no longer re-reads your whole Codex quota history every 30 seconds. Measured against a real store it fell from 1.7 seconds to 0.41, and from 29% of a rebuild to 9%.
- The dashboard's transcript sync rests at least as long as the pass it just finished, holding it at or below half a core however large the store grows. On a large store, new turns reach the conversations list up to about twice as late.

### Fixed
- `/api/doctor`, `/api/data` and the block-detail route no longer append a second HTTP response when a body write fails after the first response is committed.
- The doctor summary embedded in `/api/data` refreshes when the dashboard update thread changes its state file, so it agrees immediately with the live `/api/doctor` report.
- A dashboard refresh refused because another rebuild holds the sync lock reports `refresh busy` instead of silently returning to ordinary freshness text.
- `GET /api/data` and the dashboard's live stream no longer disagree about whether a rebuild is still filling in data. The endpoint serves the most recently published state.
- The dashboard keeps four-figure Claude, Codex and combined spend headlines readable at 320px instead of dropping trailing digits, and the desktop Daily heatmap shows its totals footer in full.
- The Codex dashboard skips a future-start weekly quota cycle instead of letting one anomalous reset fail the whole provider and log the same error on every refresh.
- The dashboard's phone hero keeps both Claude quota metrics inside the 320px usage zone, shows multi-account cards as a full-width rail, and keeps `Unavailable` or `Partial` in compact source-status warnings.
- The `All` view shows a focused account's own week- or cycle-to-date spend beside its card, including `$0.00`, instead of `no data` or an unavailable combined figure.
- A focused Codex account without its own budget points at the per-account `budget.codex.accounts` setting and names that account's key, instead of a vendor-wide command that could never update the card.
- Settings filtering announces only the settled result count instead of every keystroke, and drops an unreachable post-save notice.
- The dashboard's Forecast range summaries and all-provider Cache Report composition expose their accessible names as semantic groups instead of generic containers.
- The 5-hour block navigator in the Current Usage modal stays mounted while a block loads, so the control no longer disappears under the pointer. The prior table is replaced by a loading status.
- The dashboard's Codex Trend panel and modal describe quota history as cycles throughout — headings, labels, charts and assistive text. Claude keeps week vocabulary.
- The dashboard's multi-account Codex spend headline is no longer clipped mid-glyph at 320px.
- The Codex dashboard no longer overflows sideways when its narrow scrolled header includes an account selector. Headers at 480px and wider keep the selector.
- Arming the dashboard's phase trace no longer switches progressive first paint off, so the run you are diagnosing behaves like the one you are trying to explain.
- The dashboard's data-age figure no longer claims a freshness it does not have. A failed or degraded rebuild reports no sync time, and the error state on screen explains why.
- A dashboard rebuild blocked by a locked `cache.db` says the cache database is busy rather than reporting a generic failure.
- `cctally dashboard --sync-interval` is documented as what it is: a floor on the cooldown between rebuilds, not the period between them.

## [1.99.1] - 2026-08-15

### Fixed
- The dashboard labels the previous calendar day `Yesterday` across a fall-back daylight-saving transition, instead of showing an absolute timestamp.
- Budget figures no longer depend on which Python version runs cctally. The dashboard's four budget sums used `sum()`, which CPython changed in 3.12, so the same spend published `$49.20424485` there and `$49.204244850000016` on 3.11.
- `cctally account attribute --help` no longer prints `(default: None)` under a required argument. Python 3.13 already suppressed it and 3.11 and 3.12 did not; the 3.13 rule now applies to every required option on every subcommand.

## [1.99.0] - 2026-08-15

### Added
- The dashboard's Forecast panel and modal show your configured budget beside the quota forecast, on `All` and on both provider tabs. Nothing is composed across providers, because two budgets over two different periods do not add.
- The dashboard states which budget state you are in. No budget set, only per-account budgets, and a configured budget whose window cannot be resolved each read differently, and none of them invents a percentage or a verdict.
- You can focus an account on the `All` view, with one chip row per provider that has more than one account, cycled with `a`. A Codex focus filters every panel; a Claude focus changes the hero and the alert list only, and each row says so.
- A share taken while an account is focused carries that account under `All` too. A shared Forecast artifact also states the configured budget beside its quota projections.
- `cctally account attribute <ref> --since <iso>` moves already-recorded Codex quota windows and their spend into a known account. It previews by default and writes nothing until `--yes`, and `--retract` withdraws an assertion.
- `cctally doctor` reports Codex window-attribution assertions that match no current window, match more than one, or conflict with recorded evidence.

### Fixed
- Dashboard refreshes update only what changed instead of re-pricing each provider's whole retained history. On a 60,000-entry Claude benchmark the added build time fell from about 404 ms to 2.5 ms, with unchanged totals.
- Focusing a Claude account no longer prints the whole provider's weekly percentage as that account's own. Where the account has no weekly percentage of its own, the headline says so, matching what the rest of the hero already said.
- A Codex budget configured only per account no longer takes the whole Codex view down. A budget problem degrades the budget block and nothing else, on both providers.
- An old alert in Recent alerts shows the time it fired, not just the day: `Apr 16 14:32 PDT`, in your display timezone. On a narrow screen the timestamp takes a line of its own, so each panel row is one line taller.
- On a Codex install with more than one account, the spend headline covers the seven days it is labelled with, so it adds up to the cards beneath it. Any account counted over a wider period says so on its card.
- The dashboard index fix described in the 1.97.0 notes never reached an existing installation, and now does. A migration delivers it, so every store picks it up once on the next open.
- The dashboard refreshes in seconds rather than minutes on a large store. On a store of about 574,000 usage rows the gap between pushes was 167 to 184 seconds; the rebuild fell from about 17.9 seconds to about 10.7.

## [1.98.0] - 2026-08-14

### Changed
- Every cross-provider aggregate on the `All` view states the range it covers and carries its provider attribution. Both providers' projects are ranked over one shared absolute range, and the panel names its resolved dates.
- A ranked project row the drill-down cannot reach no longer offers one. It keeps its rank, label and cost, and has no detail affordance. A Claude project drill opened from `All` states the span it actually reported.
- Monthly is no longer merged across providers under `All`. Each provider keeps its own months in its own section, and the modal's table and navigator name the provider, so two rows sharing a label can be told apart.
- Weekly and Monthly foot their combined cost with the span it covers and each provider's share. Weekly states exact dates; Monthly states a month-label span, because monthly rows carry no bounds.
- A combined daily row no longer publishes a cache-hit percentage or a model split, because Claude and Codex count cache reads differently and their model families share no denominator. The day's detail breaks the figure down per provider.
- Five-hour blocks under `All` interleave both providers in time order rather than listing every Claude window above every Codex one. The footer states the interval shown and each provider's count and cost.
- Weekly and Monthly table sorting is per table. One shared preference applied a weekly sort to the monthly table, which has no `Used %` or `$/1%` column. An existing shared preference is reset rather than guessed at.
- When a cross-provider aggregate cannot be computed, the dashboard says so and names the missing fact, instead of showing an empty table that reads as no activity or an error that reads as a broken instance.
- A cross-provider span never names a day that has not happened yet; the stated end is clamped to the instant the page was generated. The Codex project detail states the window its own totals cover.
- Recent alerts under `All` lists both providers in one true chronological order, by the instant each alert fired. Codex rows published the moment a threshold was crossed, which is a different moment, and now publish the firing instant.
- The Codex budget alert chip reads `BUDGET` rather than `CODEX`, so it names the metric instead of repeating what the source chip beside it already says.
- The Recent Sessions panel states its composition under `All`, on the same sub-line the Weekly, Monthly, Daily, Projects and Blocks panels already use. It claims no ordering. The Claude and Codex tabs are unchanged.
- Nothing under `All` is rendered but unreachable. At 1440x900 the Current Usage and Trend modals clipped their content with no scrollbar; both now scroll as one scrollport. Weekly and Monthly are unchanged.
- The `All` Forecast panel shows each provider's own detail: Claude's two per-day budget figures, and Codex's confidence and budget pace. With more than one Codex account the confidence line stays withheld.
- A provider with no forecast is no longer reported as `unavailable` in the `All` Forecast panel. It renders the labelled dash structure its own tab renders, and keeps the sentence naming why it contributed nothing.
- The Projects window pills look disabled when they are. Under Codex and `All` the `1w`, `4w`, `8w` and `12w` buttons are inert, because provider-native project history has no week window.
- On an install with a single Codex account, the cycle navigator in the Current Usage modal is announced in cycle vocabulary. Both of its buttons were named for weeks whatever the provider.
- Every provider section under `All` is announced by a heading naming it, so the view can be navigated by heading rather than read as one flat block. Nothing changes visually.
- The Help overlay says how to open the Current Usage modal, which no digit shortcut reaches, and drops a block that could never list anything. Four false statements about `All` in the dashboard documentation are corrected.

## [1.97.0] - 2026-08-13

### Changed
- Reveal-mode share exports allow a project directory whose basename is exactly a UUID or a source-root-shaped hex token. The same value outside a typed project field still fails the document-wide privacy guard.
- The dashboard's `All` headline states a quantity you can check: the sum of each provider's own current billing period. It previously added a thirty-day Claude rollup to a seven-day Codex cycle under no period label.
- The `All` staleness marker no longer sits on the figure permanently. The hero freshness axis means the current cycle resolves and its counters are publishable; the age of the last percent observation moved to the quota axis.
- On an install with more than one account on either provider, the `All` combined total is withheld with a named reason and points at the per-account cards. A count that cannot be read withholds the figure too.

### Fixed
- Deep doctor diagnostics and `cctally db journal-repair` fingerprint structural violations at the same journal positions a rebuild uses, so one path can no longer reject another path's durable resolution audit.
- `cctally --help` and `cctally setup --help` describe both Claude and Codex, expose the provider-aware `--source` analytics and the native Codex quota commands, and name the setup-managed Codex handlers.
- The dashboard refreshes far more often on an install with a large Codex history. A per-file lookup read every Codex usage row for every rollout file — 57 seconds of an 84-second rebuild on one real store — and a new index makes it linear.

## [1.96.2] - 2026-08-13

### Fixed
- Settings section links scroll only the content pane, keep the modal chrome visible, and select the requested section consistently in Safari.
- Claude Sonnet 5 usage is priced at its permanent $2/$10 per-million-token rate, and Claude Mythos 5 and retained Mythos Preview usage are no longer left unpriced.

## [1.96.1] - 2026-08-13

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.96.0] - 2026-08-13

### Added
- The dashboard Settings overlay is navigable. A section index rail reaches any of its seven groups without scrolling, and a filter matches a setting's name, its help text, its dotted key path, or the words "this browser".
- Every one of the thirty-seven configuration keys has an on-screen disposition. Thirteen have an editor, one is read-only, three are disclosed without one, and twenty are named as CLI-only with the exact command to run.
- You can set the Claude weekly budget from the dashboard. The amount is mirrored into the dashboard's live data, which is what lets Settings tell "no budget configured" from "budget configured, alerts off".

### Changed
- Settings tells you why a save was refused, and where. The rejection lands on the control it names, and an error summary above the buttons links to each problem. Save is no longer greyed out when a field is invalid.
- A slow save says something true. Past three seconds Settings reports the elapsed time and that a large history can make this take a while. Closing the overlay is suppressed while a save is in flight.
- Sessions-per-page stops rewriting your stored value while you type. Typing `5` into a field holding `50` used to persist `10`. The field keeps what you typed and refuses to save until it is a whole number between 10 and 1000.
- Settings looks like a designed surface: every disabled control is visibly disabled and ignores the pointer, every control takes the same focus ring, and the action bar sits outside the scrolling area.
- Three Settings controls gained a real accessible name: the custom-timezone input, sessions-per-page, and the remembered filter term.
- The dashboard settings endpoint refuses a setting it cannot write instead of answering success and discarding it. Each rejection returns 400 and names the key, and the keys accepted without storing are listed in an `ignored_fields` array.
- `cctally config --help` no longer presents three keys as the supported set. It labels them as commonly set, states how many keys are settable, and points at `cctally config get` and the reference page.

### Fixed
- Share charts use an AA-compliant light warning colour, keep stacked segments distinguishable without colour, keep stacked-bar legends outside the plotted bars, and give detail charts enough height to space every session label.
- A string sent to `budget.codex.alerts_enabled` or `budget.codex.projected_enabled` through the dashboard settings endpoint was coerced to a boolean and stored, while the Claude counterparts rejected it. Both sides now answer identically.
- The configuration reference documented 19 of the 37 keys `cctally config` accepts, and now documents all of them with values, defaults and whether the dashboard can write each one.
- The `cache-report` documentation and the `--anomaly-threshold-pp` help state that the flag uses its own default of 15 and does not read the setting the dashboard writes and the dashboard and TUI read.
- The dashboard's Cache Report threshold popover says which surfaces the value it writes governs, because it is the only place that value can be set and it said nothing.
- Mobile share previews reach report content. Preset management keeps its header visible, offers explicit rename and confirmation actions, localizes save times, and traps focus in the topmost share dialog.
- Topbar chips meet the 44px hit-target size without overlapping adjacent controls.
- Share artifacts no longer describe an omitted `--since` as an all-history period above only recent content. The default start follows the first displayed day; an explicit start is unchanged.
- Share export frontmatter contains every absolute UTC instant the artifact prints, even outside UTC. West-of-UTC start bounds and east-of-UTC end bounds previously drifted past a displayed row.
- Share exports reject a non-boolean privacy or preset-overwrite field instead of reading a string such as `"false"` as permission to reveal project names or replace a saved recipe.
- Project labels use one collision algorithm across the CLI, dashboard, reporting and budget surfaces, so repeated parent names qualify far enough to stay distinct.
- Journal writes recover from a clock-skew or restore artifact when every future-dated segment is an empty file. Any segment containing durable bytes is untouched, and the refusal explains how to wait or merge it forward safely.
- A concurrent command identifies a detached stats rebuild instead of claiming an operator maintenance command is running.
- `cctally db rebuild` diagnostics name each failing journal segment and its exception class.
- Live journal ingestion removes a completed correction's materialized effects when a later conflicting marker taints that batch, so a corrected figure cannot survive its own retraction.
- A cache-repair owner is no longer reported as dead because the caller's timezone differs from the one that process started in.

## [1.95.5] - 2026-08-09

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.95.4] - 2026-08-09

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.95.3] - 2026-08-09

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.95.2] - 2026-08-09

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.95.1] - 2026-08-09

### Fixed
- Corruption recovery preserves both incidents when two database families are quarantined in the same second, instead of reusing the first incident's directory and failing.
- Concurrent usage ingestion no longer derives five-hour blocks or milestones from a snapshot that a retained suppression event deliberately removed, which used to stall every ingester behind it.

## [1.95.0] - 2026-08-09

### Fixed
- `stats.db` uses SQLite rollback journaling instead of write-ahead logging, so it no longer depends on live `-wal` files. Existing installs rebuild once on upgrade, and `cctally db checkpoint --db stats` is retired with exit 2.
- Codex Guardian approval-review usage is priced through its canonical `gpt-5.5` model instead of the generic unknown-model fallback, so those rows no longer trigger a false pricing-coverage advisory.
- Codex share artifacts disclose provider availability consistently across all nine dashboard panels. Six of them derived availability from the row count alone, so a real partial or empty provider state left the artifact unchanged.
- Corrupt `stats.db` recovery no longer starves behind continuous hook traffic. One caller becomes the durable owner, the rest return promptly and wait on it, and the rebuild happens after readers drain.
- Escape closes the share modal after you have clicked into the live preview. The preview is a sandboxed frame, so the key never reached the shortcut, and the only way out was to tab back out of the frame first.
- The share preview frame is no longer announced to a screen reader as "Report preview (decorative)" — on the one control the documentation tells you to review before sharing. It now names the panel and template it is showing.
- Manage presets is a real dialog. Tab used to walk straight out of it into the gallery behind, and closing it left focus nowhere. It now keeps Tab inside itself, fits a phone, and returns focus to the button you opened it from.
- Escape inside a composer section's actions menu closes just the menu instead of the whole composer, so your unsaved title, theme and format edits survive. Opening one section's actions closes any other section's.
- Share checkboxes and radio buttons are 44 pixels tall in a narrow browser window, not only on a touch device. The `Anon on export` label is 16 pixels on a phone, below which iOS zooms the page when you tap a control.
- The share preview on a phone shows the report instead of its frontmatter. It was capped at 128 pixels; the cap is now proportional to the visible screen and shrinks correctly when the on-screen keyboard is up.
- Gallery text no longer appears to bleed through the pinned share preview on a phone. The preview has a visible boundary and sits flush against the top of the scroll area.
- Rotating a phone or resizing the window with the share modal open no longer resets the preview, blanks it for a second, or briefly shows the wrong statement about whether your export contains real project names.
- The keyboard shortcuts in the share documentation name gestures that work. Both share shortcuts have always needed Shift, and the share icon's tooltip now says `Share (Shift+S)`.

## [1.94.2] - 2026-08-08

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.94.1] - 2026-08-08

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.94.0] - 2026-08-08

### Added
- Every share artifact states, under its title, the period it covers, the timezone those dates are in, and whether its project names are anonymized — in Markdown, HTML and SVG, from the command line and from the dashboard.
- `cctally budget --format html` and `--format svg` show the `ok`/`warn`/`over` verdict and the budget target. Both live in the artifact's totals, which those two formats rendered nowhere.
- Renaming a saved share preset keeps the preset. It was a create-then-delete pair, so a renamed Codex preset came back labelled Claude, and renaming onto an existing name destroyed that preset without asking.
- A multi-panel composed export appears in Recent shares, labelled with how many sections it contained. The composer's five export buttons previously recorded nothing at all.
- `cctally doctor` gained a `db.retained_artifacts` check reporting how much disk the retained corruption evidence occupies, how much is reclaimable and how much is protected, naming the rule that is unsatisfied.
- `cctally db prune` reclaims retained corruption evidence down to your `storage.artifact_retention` policy. It previews by default, `--yes` applies it, `--include-backups` widens it, and `--json` emits the whole plan.
- Ordinary successful commands schedule at most one background reclamation a day, so retained evidence stays inside your policy without you running anything. Set `CCTALLY_DISABLE_RETENTION_SWEEP=1` to switch it off.
- `storage.artifact_retention` bounds the on-disk evidence corruption recovery keeps, taking `max_age_days`, `max_count_per_family`, `max_total_mib`, `min_free_mib` and `max_shape_examples`. Fields you omit keep their default.

### Changed
- Share artifacts print full dates. A report covering January 2020 to May 2026 read `Jan 01 → May 09` and named neither year; period labels, titles and week, day and session cells are all full ISO now.
- `--no-branding` strips the advertisement and keeps the provenance. In Markdown it used to remove the whole frontmatter — title, period, panel and privacy mode went with the version stamp.
- The timezone a share artifact names is a concrete zone. Command-line artifacts labelled every period `(local)`, and the period is now computed in the zone it is labelled with.
- A composed multi-panel document names cctally and its version in its footer, as a single-panel one does. It read only `cctally · composed`.
- A composed multi-panel document has one top-level heading and one heading per section. When it combines Claude and Codex sections of the same report, each heading names its provider.
- Clearing the report basket, deleting a preset, and replacing one by saving or renaming over it ask before they act, naming exactly what is about to be destroyed. All four committed on the first click.
- The composer's per-section menu no longer offers "Preview only this", which was wired to nothing. The documentation describing it is corrected along with several other claims about affordances that do not exist.
- cctally now tells you which of two situations you are in when it describes `stats.db`. On an install past the journal cutover it is a disposable index that rebuilds itself; only on a pre-cutover install may it be your only copy.
- `cctally doctor`'s auto-heal check states how many corruption incidents there are and how long ago the most recent one was, and fails when three heals occur within a week or one kind of damage recurs within a week.

### Fixed
- A dark-theme share artifact prints legibly. Pale grey text reached paper at a contrast of 1.24 to 1, and the chart painted a near-black background. Text, tables, the chart canvas, its axis lines and its data colours are all corrected.
- Printing a share artifact fits the page again. The chart and the table each scroll inside their own box on screen, and on paper a scroll box is a crop, so the chart lost its right edge and a long table printed only its first page.
- A composed document exported to a dark theme has a visible title. A composed SVG also gains the title, footer and background it never had, so `--no-branding` now has something to remove.
- Chart labels no longer run off the canvas, print on top of each other, or push the page sideways. Both edges are measured and the canvas widens to fit, and an axis whose labels cannot all fit drops some rather than overprinting.
- An HTML export no longer scrolls sideways, and its chart stays readable on a narrow screen: the chart and the data table each scroll inside their own box.
- Chart text is measured by the width each letter actually takes, in every script. One estimate for every character under-reserved capitals by nearly 40%, and Chinese, Japanese, Korean and emoji labels by 30 to 50%.
- A share artifact states a period that describes what that artifact shows, which depends on the format: a weekly Markdown recap states its week, while the HTML and SVG exports state the eight weeks their chart draws.
- The `period:` field in an exported Markdown file's frontmatter ends after the last thing the export shows, instead of at midnight of that day. The dates a reader sees are unchanged.
- A share artifact is typographically one file. Only the SVG table cells named a font, so the title, period line, timestamp, footer and every chart label fell back to whatever the viewer chose.
- A chart-only Markdown export no longer carries a blank gap where its table would be, and no artifact draws a table header with no rows under it.
- The project column in a `--source`-aware report is labelled `Project` again. Its header was being anonymized along with the data, so the column was titled `project-1`.
- A shared Codex quota block states a full date rather than the dashboard's compact `13:00 May 07` chip text, which names no year.
- `cctally <command> --source all --format …` exits 3 when it refuses to write an artifact on privacy grounds, matching every single-source command. It exited 1 with a traceback.
- A basket section no longer goes "Outdated" just because a moment passed. It is compared by what the report is made of — title, period, rows, chart, totals and notes — rather than by a microsecond clock.
- An export that never happened is no longer reported as a success. A blocked pop-up and a print dialog that never opened both wrote a Recent-shares entry; both now tell you what went wrong and record nothing.
- The share modal warns you that the preview failed before you export rather than after, and the privacy line beside the buttons stops making a definite claim it no longer has a basis for.
- `stats.db`, its journal files and the `logs/` directory are created readable and writable only by you, matching `cache.db` and the data directory. Nothing was exposed by this and no action is required.

## [1.93.1] - 2026-08-07

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.93.0] - 2026-08-07

### Added
- The dashboard's share modal states, above the preview, what the export will contain: real names, anonymized names, or no project names at all. The preview always shows real names, so the checkbox used to change nothing visible.

### Changed
- A share artifact that would disclose an identifier the documentation promises it will not is refused rather than produced, and the message names the offending value. On the command line that is exit 3 with no file written.
- Anonymized session charts label each bar with its cost rank and project (`1 · project-1`) instead of the session id. A `sessions` artifact could previously carry fifteen session ids and still claim to be anonymized.
- Revealing project names shows a disambiguated basename everywhere. Two projects named `app` under different parents render as `app (work)` and `app (personal)` rather than collapsing into one label.
- A composed multi-panel document uses one set of anonymous names across all its sections, so `project-1` means the same project everywhere in the document.
- When the dashboard refuses a share export, its log records only what kind of identifier was found, not the identifier itself, because a log is a plausible thing to paste into a bug report. The message on screen is unchanged.
- `cctally db rebuild --db stats` skips re-reading journal files it has already proved are in the cache, which is most of a large install. `--json` reports which files were skipped and why each of the others was not.

### Fixed
- The multi-account dashboard header stays on one desktop row when the live sync age changes between seconds and minutes, and keeps its stacked layout at phone width.
- The `anonymized:` field in exported Markdown reports whether the export was anonymized, rather than being guessed from what the labels look like. A project genuinely named `project-1` was reported as anonymized when it was not.
- The dashboard's render and compose endpoints anonymize when the request omits the anonymization field. `POST /api/share/render` previously defaulted that absent field to revealing real names.
- An in-place stats index rebuild that overlaps an idle reader no longer corrupts the index, and corruption recovery preserves a mismatched write-ahead-log generation instead of folding it into the main database.

## [1.92.3] - 2026-08-06

### Changed
- `cctally db journal-repair` and `cctally db rederive` read the journal once per pass instead of two to four times, which lowers their memory use and start-up time on large journals. Their output is unchanged.
- Rebuilding the stats index no longer replays Codex quota history into a cache that already holds it, and a rebuild that does replay releases the cache write locks between bounded pieces. `database is locked` during a rebuild is gone.
- A rebuild whose Codex cache recovery could not finish records that its quota view is incomplete, and every command refuses to serve that view rather than showing an understated one. `--json` reports `publication` and `cacheRecovery`.
- Reading a correction record no longer makes cctally read the whole journal — on a 1.7 GB journal, every time. It continues from what the last rebuild recorded, and falls back to the full read rather than guessing whenever it cannot.
- The stats index records what a rebuild worked out about correction batches instead of working it out again on the next command. This upgrade rebuilds the stats index once, and nothing about the reports changes.

### Fixed
- `cctally doctor` reports when the stats index's quota view is marked incomplete and names `cctally cache-sync` as the way to reconcile it. The mark could previously stay set indefinitely with nothing stating the cause or the remedy.
- Every refusal to serve an incomplete quota view names `cctally cache-sync`. In the terminal dashboard the refusal is reported as its own cause rather than folded into a generic database failure that left the Codex panel blank.
- The web dashboard's status chip reads `quota view reconciling` and names `cctally cache-sync`, where it read `server sync error` with no suggested action. The two affected endpoints answer with a distinct response naming the same command.
- A corrupt stats index is no longer reported as an incomplete quota view. That misreading also stopped the corruption ever reaching cctally's repair path.
- Publishing a rebuilt stats index no longer deletes the write-ahead log files of a database other cctally processes still have open, which could make those processes read stale figures or fail with a disk I/O error.
- Finishing an interrupted Codex cache recovery no longer makes an ordinary command read the whole journal. Only `cctally cache-sync` and the dashboard attempt it now; every other command reads the mark and moves on.
- A rebuild holds its read view of `cache.db` for a fraction of a second instead of about 28 seconds, so it no longer blocks compaction of the Codex cache's write-ahead log for that long.
- A cctally process interrupted just before a month boundary could append to last month's journal file after this month's had started. Both writers re-check which file is current when they take the write lock.
- A crash during the first conversion to the journal format could leave a duplicate copy of the conversion file behind, which every later rebuild then read. On one real install two duplicates accounted for 366 MB.

## [1.92.2] - 2026-08-06

### Changed
- Rebuilding the stats index uses about half the memory it used to. On a 1.7 GB journal peak memory dropped from 9.0 GB to 4.6 GB. It does hold the Codex cache write lock 18% to 38% longer.

### Fixed
- A readable but structurally damaged stats index no longer traps an upgrade or recovery in an endless rebuild loop that left the dashboard at `server sync error` and made every block report approximate.

## [1.92.1] - 2026-08-06

### Changed
- A rebuilt stats index is published into the existing database file inside one transaction, so reports and the dashboard keep working while a rebuild publishes. No quarantined copy is left behind; use `cctally db backup --db stats` first.
- Recovering a corrupt stats index no longer makes you wait for it. The command that finds the damage writes the evidence, schedules the rebuild in the background and exits. `cctally db rebuild --db stats` still waits for it.

### Fixed
- The Codex Current Cycle modal no longer presents its native-quota label as a second provider identity, keeping Codex on one app-wide accent colour.
- Account-filtered Codex conversation lists keep each conversation's project grouping and badge instead of relabelling every scoped row `(unassigned)`.
- A conversation permalink keeps the selected conversation visible and marked current even when it falls beyond the rail's first 50 rows.
- Conversation Viewer find counts and reaches text inside Codex injected-context, skill, command, compaction and notification bodies, opening only the disclosure that owns the match.

## [1.92.0] - 2026-08-05

### Added
- A non-loopback dashboard requires a per-run bearer token, bootstrapped into an HttpOnly cookie by the browser, with a restart-only opt-out from Settings or the CLI.
- Conversation browsing, search, reading, export and permalinks are account-scoped, in the dashboard and in `cctally transcript`. The existing all-account and single-account output shapes are preserved.

### Changed
- A Codex tool result in the conversation reader no longer opens with the harness's own `Script completed` / `Wall time` / `Output:` boilerplate, and every call ends with an explicit **ok**, **error**, **running** or **outcome unknown**.
- Four more Codex tool families are legible rather than raw: a JavaScript `exec` body and every `js` call show the invocations that could be decoded, `write_stdin` and `wait` name their session, and `tool_search_call` shows its query.
- A Codex patch event shows a per-file diff, including for added and deleted files, labelled as rendered from retained content. A diff clipped to nothing says so rather than claiming none was retained.
- A conversation's shell sessions are numbered and grouped, and the call that opened one is marked. Session numbers are conversation-local, and the provider's own session identifier is never shown.
- The Codex conversation outline lists each authored reasoning heading, each failing tool call and each plan call as its own row. On the heaviest conversation in a real store that is 424 landmarks where there were none.
- The Codex outline's stats card reports real models, tools, duration and errors instead of leaving four rows blank, and says `not reported` where the evidence cannot say whether anything failed.
- The Codex outline's Files tab lists each file a patch touched with its own `+N −M`, and a file row jumps to the change that touched it. A count that cannot be determined is left blank rather than understated.
- Conversation titles stop showing the harness's own markup — a skill invocation's private path, a slash-command wrapper — on every surface that renders one, including `cctally transcript search --source codex --kind title`.
- Long Codex conversations open with two concurrent data requests instead of three. On a 3,733-row conversation the first painted row improved from 784 ms to 421–429 ms, with live-tail streaming still connected.

### Fixed
- A missing, empty, unrecognized or temporarily unmounted `$CODEX_HOME` no longer makes an ordinary sync or rebuild erase retained Codex accounting and conversation history. Every safety refusal is reported through `cctally doctor`.
- `cctally setup` no longer adds duplicate Codex `Stop` and `SubagentStop` hooks on every npm install, and reconciles the existing ones instead. Reported independently by @darlingm, who also supplied a regression patch.
- Codex spend and quota ingest no longer freeze after the first append to a tracked rollout when a large history begins with legacy record envelopes.
- Cache Report no longer warns that net cache spend is negative when savings and write costs cancel to an effectively zero floating-point residue.
- Cache Report charts announce one consistent window size to screen readers and identify unobserved days as a measured subset.
- Codex quota rows restored from the journal or repaired during migration invalidate cached dashboard and projection state immediately, instead of waiting for an unrelated later Codex mutation.
- Codex file-path search indexes the patch events the provider actually records, and an upgrade backfills retained history so existing conversations become searchable by touched file.
- Codex quota alerts no longer leave a future-clocked capture unevaluated forever when its window goes quiet. The first hook tick after it matures re-evaluates only the root that owns it.
- A detached Codex quota verification can no longer make the following hook tick claim success without evaluating its budget alerts. The tick stays due and retries once the stats ingest lock is released.
- A stats index rebuild that publishes a damaged index reports failure instead of printing a success line, and the next command refuses that index. A corruption incident also retains more evidence about itself.
- A conversation's injected-context rows are readable on a phone. Labels such as `SESSION CONTEXT` were being broken apart mid-word, one or two characters per line, and now wrap at spaces.
- A file path, a relative path or a custom link target in conversation text is no longer rendered as a working link or image. The browser was quietly requesting some of them from the dashboard. Web and email links are unaffected.
- Codex and Claude each have one colour across the whole app. The same blue previously meant Claude on the dashboard's source chips and Codex in the conversation viewer. Codex is green and Claude is purple everywhere.
- Opening a conversation link selects the matching source tab and highlights the conversation's row even when an active search or filter would have hidden it, and Back returns to where you were rather than to an intermediate list view.
- Going Back or Forward keeps the search and filters you had set, rather than clearing them as following a fresh link does. Opening a parent, child or subagent thread stays within the account you were viewing.
- Stepping through Codex landmarks with `e`/`E` or the jump chip moves. It used to re-find the first landmark forever going forward and report none going backward, and the rail's highlight now follows whichever control issued the jump.
- A failing Codex tool result shown on its own can be jumped to at all, is shown as failed rather than as though it had succeeded, and says when its output was clipped. A patch the provider reported as `error` counts as a failure too.
- The Errors filter's count agrees with the jump chip beside it: it read the number of failing calls — 27 next to a chip reading 14 — and now reads the number of error turns at every width.
- An MCP tool that reports a protocol-level error inside a successful Codex transport response appears as failed in conversation cards, the Errors filter and count, and outline landmarks.
- A subagent run reporting `failed` is shown with the error marker rather than the neutral one, so those runs reach the Errors filter and badge.
- Conversation Viewer outline pins stay truthful after restoring a saved position inside a tall turn and after jumping to a cache rebuild on a sidechain member.
- The Conversation Viewer preserves Claude tool names and request and result details on qualified links, keeps terminal copy controls clear of long working directories at phone width, and uses one set of outcome words everywhere.
- A qualified Claude conversation link keeps its error, subagent, plan, compaction, file and cache-rebuild navigation, and a one-segment Codex link opens under the Codex source instead of Claude.
- Codex conversation search shows readable retained tool output instead of serialized `[{"text": ...}]` wrappers. Opening the Codex conversation list is also much faster: a warm 50-row browse fell from 1.334 seconds to 0.162.
- Find in a Codex conversation counts and reaches every visible match instead of only the messages that contain one. Next and previous cross the complete result set. Claude find behaviour is unchanged.
- A conversation deep link finishes only after the requested message is mounted and stably aligned at its start, and a target that cannot be held in view produces a visible failure instead of silently clearing the jump.
- Pressing `h` or `H` in a long Codex conversation reaches the adjacent reasoning heading even when that heading's row is unmounted behind a very large turn. A failed hop keeps the existing heading marked.
- A failed Conversation Viewer page request during a deep-link or find jump retries with bounded backoff and then shows a connection error, instead of spinning until the tab runs out of memory.
- Dashboard conversation source controls and provider context render their intended borders and active surface, stale source status keeps its outline, and Codex reasoning uses the dashboard prose typeface.

## [1.91.0] - 2026-08-03

### Added
- The Codex conversation reader shows an indicator inside the transcript while a further page is loading, so a scroll-up or a jump that has to fetch is visibly in progress. A jump that cannot reach its target now says so.
- Codex reasoning lists every heading the model actually wrote, one readable line each, instead of a single clipped line. Press `h` and `H` to step through them, including into a part of the conversation that has not loaded yet.

### Changed
- A long Codex conversation opens without downloading the whole thing: the server serves a bounded page and the reader loads the rest as you scroll. The heaviest conversation in a real store went from 13.2 MB to 2.5 MB.
- A very long Codex turn is split into several bounded reading units instead of one enormous message, so the reader can scroll it smoothly. Deep links, bookmarks and saved reading positions issued before this change still resolve.
- A Codex message that is not the one its turn's cost is attributed to reports no cost at all rather than a zero, so a JSON consumer can tell "carries no cost of its own" from "cost nothing". No displayed figure changes.
- Consecutive Codex messages read as the separate messages they were, instead of being run together into one block of prose.
- The dashboard's Codex Cache Report wire publishes only `cached_input_percent`, reports structurally inapplicable wasted-cost and efficiency values as `null`, and identifies the change as source schema version 4.

### Fixed
- The Codex hero's transient ingest-backlog note stays on one compact line at phone width. The full `+N sessions still loading` wording remains on wider screens and for assistive technology at every width.
- Multi-account Codex quota labels stay attached to the account they came from when two accounts share one `$CODEX_HOME` root. Single-account dashboards keep their existing join.
- The dashboard's All tab keeps its combined spend and token total when one provider's quota evidence is stale. The retained actuals carry a visible `Stale quota` marker, and forward-looking projections stay paused.
- Multi-account Codex cards mark stale quota evidence on the account it belongs to. A fresh sibling no longer hides another account's staleness, and the stale card keeps its retained percentage, reset and spend.
- The dashboard's Recent Alerts card and modal identify which account each alert belongs to and honour the account filter, keeping vendor-wide crossings as `All accounts`. Same-threshold alerts no longer collapse into one toast.
- The dashboard marks incomplete Codex totals everywhere they are shown. The Combined spend hero carries the same `+N sessions still loading` caveat as the Codex hero, and the current-cycle modal explains that its totals will rise.
- A deep link, bookmark or saved reading position pointing into a long Codex turn loads the message it names. Any position inside a turn the outline omits resolved to nothing at all, and the reader stayed put showing no error.
- Find in a Codex conversation reports and reaches every match. Matches past a long turn's first reading unit were dropped: one query reported 2 matches where there are 16, and none of the dropped ones could be navigated to.
- Paging up through a Codex conversation returns the page immediately before your cursor. It returned the conversation's opening messages instead, which made scrolling up appear to jump to the beginning.
- `cctally transcript export` and the dashboard export no longer repeat each Codex reasoning heading, so an exported Codex transcript reads once through rather than twice.
- The transcript paging indicator no longer sits under the "↓ N new" pill on a narrow screen.
- A long Codex reasoning heading wraps instead of being cut off mid-sentence with no way to see the rest.
- A Codex message that opened a code block and never closed it no longer swallows the messages that follow it into that code block.
- Concurrent first-time commands no longer misclassify a freshly initialized `stats.db` as a database written by a newer cctally. A waiting opener rechecks the index epoch after acquiring the initialization lock.

## [1.90.1] - 2026-08-02

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.90.0] - 2026-08-02

### Fixed
- The first interactive command after a `stats.db` index-epoch upgrade no longer waits for the whole journal replay. One background rebuild starts, the status line keeps working, and other commands return retry guidance.
- Sharing a Codex panel while focused on one account keeps that account scope through the live preview, every export action, recent-share history, the report basket and composed reports. Switching the selector mid-dialog no longer changes it.
- `cache-report --json` identifies anomaly predicates that were not evaluated, so a consumer can tell an evaluated-clean row from a check skipped for a thin baseline. The field is additive and `schemaVersion` stays 1.
- Cache Report charts share one day-slot axis, so the sparkline's Today marker lines up with the Today net bar. The expanded report keeps its labelled table to 641px, switches to cards at 640px, and fits 320px without overflow.
- The Cache Report anomaly threshold means the same thing everywhere. A `cache_report.anomaly_threshold_pp` that is not a whole number between 1 and 100 falls back to the documented default of 15 on every surface.
- Cache Report no longer drops a day from the baseline when the clocks change. The window is counted in calendar days, and the "Building baseline · N/5 days" count is taken from the rows the comparison used.
- Cache Report cache-dollar totals no longer depend on the order entries happen to be read in, so the "net spend went negative" warning stops flipping between runs of the same data. One rendered cent can shift.
- On a machine whose timezone is not UTC with no `display.tz` set, Cache Report no longer mixes up which day is today. The days were grouped locally while "today" was worked out in UTC.
- The Codex Cache Report no longer labels an older day "Today" and no longer publishes a fabricated 0% for a day with no Codex activity. An idle day is marked unmeasured, as it already was for Claude.
- The Codex Cache Report no longer reports "net spend went negative" as a permanently unevaluated check. OpenAI charges no cache-write premium, so an ordinary Codex day reads as clean rather than partially checked.
- The Codex Cache Report publishes its figures under Codex's own vocabulary — "cached input" rather than Claude's "cache hit" — and states which figures do not apply to Codex, and why.
- The dashboard no longer invents a Codex Cache Report when the server published none. The modal's stand-in seeded the settings gear with a 5pp threshold the server never set, so saving there rewrote the global threshold.

## [1.89.2] - 2026-07-31

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.89.1] - 2026-07-31

### Fixed
- Cache Report no longer shows a healthy measured 0% on a day with no activity yet. The panel, spotlight, daily rows, sparkline and net-$ bars mark an unmeasured day explicitly.
- A Cache Report daily row whose anomaly predicates could not be evaluated renders a neutral flag with an explanatory legend, instead of a green check.
- A Cache Report single-source view shows a provider's degraded or stale status chip, matching what the all-sources view already showed.
- The all-sources Cache Report summary no longer reports an anomaly for data the panel beside it calls "building baseline".
- An empty Cache Report source no longer renders KPIs and a healthy verdict alongside its empty label.
- A Cache Report that failed to build says so, instead of showing an indefinite loading state.

## [1.89.0] - 2026-07-31

### Changed
- Upgrading rebuilds the local statistics index from the journal on first open, twice over. This is the automatic resolution for an index-version change and needs no action; your usage history is unaffected.

### Fixed
- The Conversation Viewer shows the real response of an MCP tool call that Claude Code moved to the background. Those calls previously showed only the harness placeholder, and the answer never appeared even though it had arrived.
- Repairing that history costs a one-time background re-read of your Claude session logs on upgrade. It briefly restores transcripts older than your retention window, so the retention sweep runs straight afterwards.
- Where a background task id is claimed by more than one call, or its completion cannot be identified beyond doubt, cctally leaves the placeholder in place rather than risk attaching an answer to the wrong call.
- A call moved to the background that never came back no longer reports itself as successful. It read `✓ ok` directly above text saying it had not finished, and now reads `running in background`; a recovered response says when it completed.
- An expanded Codex response can be collapsed again. "Show full response" expanded it and then removed itself, leaving no way back to the clamped view. It is now a toggle that stays put and reports its state to screen readers.
- A Codex conversation opened from a search hit or an outline entry no longer renders in reverse, with the final response at the top. Nothing about the stored transcripts changed, only how a page of them was cut.
- The Codex hook no longer blocks every turn for seconds on end, or times out on a large history. It reconciled the whole quota history twice per turn; on a 212,000-observation store a steady-state tick fell from about 4,000 ms to 250–350 ms.
- Reading new Codex session files during a hook is time-bounded and resumable, under `codex.hook.ingest_budget_seconds` (default 5). The session the hook fired for is read first, and `cctally cache-sync --source codex` is never budgeted.
- The first Codex hook after upgrading no longer runs long enough to hit Codex's hook limit. The index rebuild that an upgrade triggers now runs in the background, so your Codex quota numbers may lag by a turn or two on those occasions.
- A one-time full re-read of Codex history no longer leaves the hook stuck forever on an install that only ever runs the hook. The hook starts the re-read in the background, and `cctally doctor` reports it after an hour outstanding.
- The dashboard's Codex hero announces the explanation behind its spend figure to a screen reader. The description was attached to a container assistive technology does not read a label from, so it was silently unreachable.
- `cctally doctor` reports when the background pass that verifies your Codex quota projection stops landing. The new `codex_quota_verification` leg warns after a full day with no completed pass, and any non-hook command runs it immediately.
- Turning Codex quota alerts on, or upgrading with them already on, arms them reliably and does not leave every Codex turn re-reading your whole quota history afterwards.
- Codex quota windows whose model was never recorded are resolved once and stored. A separate model allowance such as GPT-5.3-Codex-Spark could otherwise be filed as ordinary account weekly quota. Existing history is repaired on upgrade.

## [1.88.2] - 2026-07-31

### Fixed
- Codex costs for the GPT-5.6 Terra and Luna models use OpenAI's current published rates. Terra was priced about 25% high and Luna about five times high. Costs are recomputed on every read, so corrected figures appear with no rebuild.
- cctally stores one rate per model rather than a dated price history, so Terra and Luna usage from before the cut is now valued at the new rates as well. Budget alerts already recorded keep the amount they were recorded with.

## [1.88.1] - 2026-07-31

### Fixed
- Session names no longer disappear from Recent Sessions while cctally prunes old transcripts. The prune now holds the transcript store's maintenance lock shared for its pass, so read-only lookups are not locked out.

### Security
- The build-time `postcss` dependency moves to 8.5.25, clearing a path-traversal advisory (GHSA-r28c-9q8g-f849) in its source-map loading. It is used only when building the dashboard from source, so it is absent from every install.

## [1.88.0] - 2026-07-31

### Fixed
- The Codex hero's Snapshot chip no longer reports stale evidence while the dashboard's freshness for the same quota window reads fresh. `active[].captured_at` now always means evidence recency.
- A focused Codex account no longer reads freshness frozen at page load, and an install tracking more than 250 quota windows can no longer publish a live row whose history was dropped. The source envelope moves to `source_schema_version` 2.
- Codex sessions started outside the Desktop app — through the MCP server, the CLI, `exec`, subagents and older builds — appear in the Conversation Viewer and resolve their project. History is re-read once on upgrade.
- That re-read briefly restores transcripts older than your retention window, so the retention sweep runs straight afterwards. A session whose log Codex has since deleted is no longer counted, so long-run totals can fall slightly.

## [1.87.2] - 2026-07-30

### Fixed
- The dashboard no longer freezes provider data while Claude or Codex sessions are continuously active. A completed read publishes the snapshot it actually built even when newer session bytes arrive before the build finishes.

## [1.87.1] - 2026-07-30

### Fixed
- Codex session names stay visible in Recent Sessions when an individual account is selected. Selecting an account replaced every session name with an em dash. Disabling transcript visibility still hides names in both views.

## [1.87.0] - 2026-07-30

### Fixed
- A Codex cycle's per-percent milestone ladder no longer reports `$0.00` for a crossing whose spend is real. Spend inside an account-level weekly window is attributed to that window's account when it names exactly one.
- Nothing is guessed there: spend no window can name is left alone, so is spend claimed by two windows naming different accounts, and a separate model pool is never treated as account quota. History is repaired on upgrade.

## [1.86.0] - 2026-07-30

### Fixed
- Each Claude, Codex or future-provider logical model release gets a distinct, accessible dashboard colour that stays consistent across cards and modals, while dated and capacity-qualified variants of the same release share one colour.
- Codex quota windows whose reported reset drifts through a chain of nearby values converge into one physical window. A cache migration repairs already-upgraded history without changing the raw provider evidence.
- A pooled Codex weekly row names every account whose near-identical reset boundary contributed to it, instead of showing one account's maximum percentage with another blended in. Focused and single-account views are unchanged.
- The dashboard keeps account focus consistent across every account-sensitive surface. A focused Claude account uses only the fields its own card provides, and merged Codex block rows name their account.
- Long account labels stay identifiable on 320px screens, account-selection announcements name the selected state correctly, and a small non-zero dollar value keeps its cents instead of rounding to `$0`.
- A weekly percent milestone is no longer recorded carrying the cost of an earlier crossing, which left two percentages at an identical cumulative figure permanently. The next observation records it with its real cost.
- Codex quota percentages appear only for windows the server still marks live. The Current Cycle modal no longer turns a retained, already-reset history row into a current percentage or `$ / 1%`.
- Codex cache sync stops before reading rollout files when its durable account-attribution decisions cannot be replayed, so a transient failure cannot reassign usage to the account currently signed in.

## [1.85.1] - 2026-07-29

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.85.0] - 2026-07-29

### Fixed
- `cctally db rederive` no longer deletes the history it cannot re-derive. A single `db rederive --family claude-usage --yes` erased months of weekly usage and cost, collapsing the dashboard's trend card.
- On an install that already lost history, the fix is the cure: run `cctally db rederive --family claude-usage` and apply it with `--yes` to restore those weeks from the journal's own retained lines.
- Rebuilding `stats.db` restores only the newest open 5-hour block for each Claude account. It could previously recreate every older snapshot window as an open block, which left the dashboard on `server sync error`.
- The Codex current cycle shows its per-percent milestones again. The hero published the raw provider reset while the milestone rows used the settled one, so the cycle read `0 crossed` over intact crossings.

## [1.84.1] - 2026-07-29

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.84.0] - 2026-07-29

### Added
- Selecting a Codex account in the dashboard re-scopes the whole Codex view to that account: periods, sessions, projects, the trend chart, cache diagnostics, the forecast, budget, quota blocks and alerts, including each panel's expanded view.
- Under "All accounts" the headline is the merged spend and token total, while the percentage, reset, `$/1%`, forecast and week range stay blank with a `per account` pointer, because independent quota allowances are never blended.

### Fixed
- One Codex quota window renders as one window. OpenAI reports the same reset a few seconds apart from sample to sample, and each spelling was its own window. Existing history is re-grouped on upgrade.
- A Codex weekly window whose reported length is one minute off its native length no longer shows up as a second, separate window. A window on a separate model allowance keeps its own pool and is never merged into account quota.
- Rebuilding the cache no longer re-labels Codex history with whichever account is signed in. Each rollout's account is decided once when its bytes are first read and recorded durably in the append-only journal.
- Signing in as a different Codex account mid-session attributes that session's later usage to the new account, without moving usage already recorded under the previous one. Older history stays unattributed.
- A truncated or half-written Codex `auth.json` stops cctally guessing an account, and `cache-sync` says so on stderr while `cctally doctor` reports it as `accounts.codex_identity`. Codex spend and quota can no longer stop updating silently.
- With more than one Codex account, "Spent this week" and combined spend add up every account instead of showing one account's. Opening the cycle from that headline shows a per-account table naming every account.
- The dashboard's Forecast panel and expanded view no longer present one Codex account's forecast as the whole picture. The expanded view lists every account with its own projection, quota, confidence and verdict.
- A Codex account whose quota week has already reset shows `—` in the Forecast surfaces until it is observed on a running window, instead of printing its last recorded percentage as a live figure.
- The dashboard's Blocks panel lists every Codex account's 5-hour blocks instead of only one account's, and each merged row names the account it belongs to.
- The dashboard's All-providers view no longer presents one Codex account's quota as the whole picture. `CODEX 7-DAY`, its reset countdown and the `Codex quota` row all read `per account`, and the per-account cards appear here too.
- Opening a past Codex cycle with an account selected shows that account's own cycle and correctly identifies which of its cycles is current. It previously read back across every account on the same Codex home.
- With a Codex account selected, its weekly percentage, `$/1%`, quota milestone costs and milestone history are genuinely its own. Usage stamped with an unknown account is shown as its own group, not dropped.
- Opening a Codex quota window from the dashboard shows every sample in that window, and no longer fails to open at all. The detail view matched samples on the raw provider reset while the window had moved to its settled one.
- Codex account cards no longer read "resets in 2d 2h ago". Every card with a future reset rendered its countdown as though it were an elapsed age.
- A sub-dollar weekly spend no longer displays as `$0`. Per-account Codex spends are routinely under a dollar, and amounts under a dollar now keep their cents.
- Two accounts carrying the same email address no longer display under the same name. cctally appends the plan — `you@example.com (pro)` — falling back to a short key fragment. A label you set yourself still takes precedence.
- Sharing a Codex panel while one account is selected exports only that account's rows. The share was labelled with the selected account while its body contained every account's data.
- A Codex quota alert you have already been shown is not forgotten when the local index is rebuilt, and is not sent a second time after a window is re-grouped. Rebuilding still sends nothing.
- Upgrading no longer stalls on the first command after a large Codex quota history is re-grouped. Settling each window's reset scanned every previously settled reset; the lookup is constant-time now and the grouping is unchanged.
- `cctally doctor` detects `conversations.db` corruption, and `cache-sync --rebuild` preserves and rebuilds the complete transcript store without manual file deletion.
- Dashboard transcript failures no longer claim `cache.db` corruption or expose raw SQLite paths, SQL or exceptions. Accounting, quota, session and live-update surfaces stay available.

## [1.83.1] - 2026-07-28

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.83.0] - 2026-07-28

### Added
- `cctally db journal-repair` records an append-only operator audit for structurally invalid correction batches and rebuilds the disposable index. It previews first, and recovers safely after an interruption.
- `cctally db rederive --family claude-usage` audits and corrects wrong journaled Claude-usage decisions in one append-only batch. `--yes` applies it, repeated application is a no-op, and replay never fires historical alerts.
- Dashboard source envelopes publish `hero`, `quota` and `sessions` freshness independently, so an idle quota window can age without making unrelated accounting or Sessions data look stale.
- `cctally doctor` reports a `journal.writer_guard` leg naming unsanctioned writes to the stats index.

### Changed
- `stats.db` auto-heal, `db rebuild --db stats` and `db repair --db stats` decline while another process holds the database open. The usual holder is a running dashboard, so stop it first if a repair declines.
- Claude dollar and token totals are documented as transcript-derived lower bounds. Claude Code bills title-generation and side-query calls whose model and token fields are absent from normal transcripts.

### Fixed
- Claude cost no longer undercounts 1-hour prompt-cache writes. Every cache write was billed at the 5-minute rate while Claude Code writes its main-session cache with the 1-hour TTL, so the cache line read about a third too low.
- Figures recomputed from your session history correct themselves on the next sync; recorded weekly, block and milestone figures keep their original value. One sync after upgrading re-reads your whole history.
- Claude responses retained as effective fast mode use the correct rate everywhere. Current Opus 5 and 4.8 fast rows use $10/$50 per MTok; retained Opus 4.6 and 4.7 fast rows keep $30/$150.
- Percent milestones preserve the first retained crossing's capture time, cost and alert latch across a stats-index replacement, so a later observation cannot fire the same alert twice. Upgrading rebuilds the index.
- A weekly cost snapshot derived while recording a milestone keeps the triggering observation's exact window, account and cost boundary. A reset that rounds across UTC midnight stays on one week key.
- Closed five-hour blocks are frozen as one deterministic fact at the first retained successor or expiry observation, so a crash, a late session row or later cache growth cannot rewrite their totals. Open blocks stay live and mutable.
- Disabling quota alerts records a durable disarm, so rebuilding and re-enabling above a threshold cannot dispatch historical alerts.
- `cctally doctor` distinguishes a rebuildable stats-index mismatch from one with no journal data, and warns when the live data directory is confirmed inside a file-level backup or sync folder.
- Codex quota history identifies exactly one live cycle when two native identities reset near the same boundary, and the dashboard history cap keeps the freshest separate model-pool fact without folding it into account quota.
- Dashboard current-cycle labels render Claude's full week range once and distinguish a clipped Codex history range from its later nominal reset, so the two provider facts no longer look contradictory.
- Cache auto-heal quarantines and rebuilds `cache.db` only when its locked forensics probe confirms corruption. A transient trigger against an integrity-clean family preserves every file and records where it came from.
- An account-budget label, email or key-prefix write attempted during `stats.db` maintenance reports the maintenance hold and asks you to retry, instead of claiming that no accounts have been observed.
- Dashboard source refreshes no longer hold a long-lived read transaction on `stats.db`, which defeated SQLite's checkpointing and made the write-ahead log grow while the dashboard was active.
- The dashboard detects corruption that first appears in a real query after its opener probe, heals once from the journal and retries. Where healing is unsafe it still binds and names the stats repair action.
- The stats-writer guard throttles violations across processes and rotates its log at 1 MiB, and `cctally doctor` reads only a bounded tail, so a multi-process violation storm cannot create an unbounded log.
- A structurally invalid journal correction batch no longer stops `stats.db` rebuilding or the dashboard starting. The batch is omitted whole, and `db rebuild --json` and `doctor` name it and its violation.
- A current-week shared report keeps provider spend visible while labelling stale hero-cycle evidence, in Markdown, HTML and SVG. A freshness-only change marks an existing composer section outdated without invalidating unrelated panels.
- The dashboard's default 1440px layout gives Recent Sessions titles two uniform readable lines instead of about 13 characters, while keeping Cost inside the card. The mobile layout is unchanged.
- A stale migration-failure banner clears after `cache.db` recovery or a rebuild durably stamps that migration applied. A genuine unresolved migration failure stays visible.
- A bare npm update on the beta channel resolves the live effective beta target before choosing a version, so a fresh release cannot be hidden by the 24-hour cached check. Explicit pins, stable npm and Homebrew are unchanged.
- `cctally cache-sync --rebuild` no longer appears to hang forever after ingest completes. The transcript rebuild reports provider, phase, elapsed time and file progress, under a 30-minute no-progress ceiling that active files refresh.
- A completed journal correction rebuilds the derived stats index on the next ingest when replacement is safe. Where a dashboard still holds the database open, cctally leaves the index alone and names both remedies.
- An interrupted `stats.db` rebuild no longer removes the current index before its replacement is ready, and one that left an empty or missing index self-heals from the journal on the next open.
- `db vacuum --db stats` took the cache maintenance lock instead of the stats one, so a full rewrite could run at the same time as a stats ingest or rebuild. `db checkpoint --db stats`, `db skip` and `db unskip` took no lock at all.
- Replacing the `stats.db` file now verifies the database family has drained and blocks new readers during the cutover, instead of replacing files under live SQLite handles.
- A `stats.db` repair or rebuild running in the background no longer parks every other cctally command indefinitely. They report that maintenance is in progress and exit instead of hanging.
- Codex session names on the dashboard follow the transcript privacy gate: a LAN viewer without transcript access sees a dash in both the Codex and All views. Shared snapshots no longer publish prompt-derived labels.
- Multi-account reset and credit detection compares each Claude account only with its own prior usage, so one account's percentage drop cannot be misread as another account's reset or goodwill credit.
- A separate Codex model pool such as GPT-5.3-Codex-Spark is no longer filed as account-level weekly quota. It created a phantom cycle that truncated the real one and skewed the weekly card, forecast and blocks.
- The Codex cycle modal's week navigation no longer disables itself when the newest retained cycle is not the current one.
- Codex cycle detail renders its retained per-percent milestones and 5-hour blocks, which had been empty for every cycle since multi-account support landed.
- Opening a Codex cycle shows the same date range and current-or-historic state the cycle list shows. A cycle cut short by an early reset opened with its full seven-day range and claimed to be current.
- The hero modal's week and block navigation arrows meet the 24×24 minimum tap-target size on a phone, with no change to how they look.
- The dashboard starts again after upgrading on a machine whose history contains a rare recording conflict. The rebuild completes, keeps the first version it recorded, and tells you which facts were ambiguous instead of refusing outright.
- Recording a fact that would create such a conflict is prevented rather than written: the check runs before the write instead of after it. A rare duplicate recording also no longer re-runs on every refresh.
- `cctally doctor` reports ambiguous recorded facts as a warning naming each one. `cctally db rebuild --db stats` lists the same groups and exits successfully, and `cctally db rederive --family claude-usage` resolves them for good.

## [1.82.1] - 2026-07-24

### Changed
- The public README is a shorter, screenshot-led tour, and it refreshes itself on every stable release: promoting a release regenerates the screenshots against that exact version and updates a "Latest stable" block automatically.

### Fixed
- The dashboard's Recent Sessions card shows session names again. Splitting transcript storage into its own database dropped the name lookup, so every row in the Session column had rendered a dash since then.
- The All tab's Recent Sessions rows show Claude session names too, matching the Codex rows beside them. Names still appear only for a local viewer, exactly as on the Claude tab.
- The Codex tab no longer blanks `SPENT THIS WEEK`, `$/1% used`, `$/1% vs last week` and the week label after Codex has been idle for an hour. The spend was never lost, just hidden; the Snapshot chip names the reading as stale.
- Forecasts still pause on stale Codex quota evidence, on the dashboard and in a shared report: `Forecast @ reset` and a shared forecast's `Projected` column show `—` rather than projecting from an hour-old reading.
- The All tab keeps withholding its combined total while a provider's quota evidence is stale, but says that is the reason instead of reporting a generic degraded state.

## [1.82.0] - 2026-07-24

### Added
- Embedded pricing for Claude Opus 5, so its sessions are costed instead of contributing $0 as an unrecognized model. It ships at the standard Opus rate with the full 1M-token window and no long-context premium.

### Fixed
- `cctally statusline`'s context segment measures a `claude-opus-5[1m]` session against its real 1,000,000-token window instead of falling back to the 200K family default, which inflated the reported context percentage roughly fivefold.

## [1.81.0] - 2026-07-24

### Added
- cctally tracks usage per account for each provider. If you use more than one Claude or Codex account on this machine, each account's percent, 5-hour and quota milestones — and their alerts — are recorded and fire independently.
- `cctally account list|show|label` shows every observed account with its provider, label, email, plan, first and last seen, and which is active, and lets you set a durable friendly label that survives a stats rebuild.
- Optional per-account weekly budgets through `config set budget.accounts` and `config set budget.codex.accounts`. A budget targets an immutable account even after you rename its label.
- `cctally doctor` gains an Accounts section reporting whether the active identity is readable, the registry is consistent, and usage is landing under the correct account.
- `--account <ref>` scopes a command to one account, by label, email or account-key prefix. It works on the Claude usage and analytics commands and the `codex quota` views, and `--json` gains `accountKey` and `accountLabel`.
- The dashboard gains a multi-account view: an account chip row under the source switcher, cycled with `a`, a per-account hero card under "All accounts", and a focused view when you select a chip.

### Changed
- Multi-account is byte-stable. If you use a single account per provider, every report, alert and status line is unchanged. Account labels and columns appear only once a provider has more than one real account.
- Alert notifications gain a `[label]` prefix only when a provider has more than one real account, and `alerts.log` records the account on every line.
- `record-credit` records the credit under the currently active account, and asks you to retry if that identity cannot be read mid-write. `sync-week` attributes its cost snapshot to the active account.

### Fixed
- Concurrent Claude and Codex hook syncs serialize every `cache.db` write and checkpoint through one lock order, closing the cross-provider overlap consistent with the recurring page-one corruption.
- A corrupt `cache.db` rebuilds without manual file surgery, including corruption discovered after open and a repair interrupted at any phase. `cctally doctor` reports live against stale maintenance.

## [1.80.4] - 2026-07-23

### Fixed
- Codex Recent Sessions keeps MCP-invoked sessions in true last-activity order and derives each Started time and duration from retained accounting events, so a rebuild no longer gives every session one date.

## [1.80.3] - 2026-07-23

### Fixed
- Cache recovery detects a damaged `session_entries` B-tree even when `cache.db` still has a readable schema, so the dashboard and `cache-sync --rebuild` can quarantine and recreate the corrupt cache.

## [1.80.2] - 2026-07-23

### Fixed
- The dashboard no longer crashes with a native SQLite `SIGBUS` when a corrupt `cache.db` is detected while another reader is active. Recovery refuses to replace a family with live handles.
- Re-reading the same retained Codex quota observation produces a byte-identical journal record instead of a new identity, which had made cache recovery multiply historical quota records and every later rebuild slower.

## [1.80.1] - 2026-07-23

### Fixed
- A corrupted `stats.db` index auto-heals correctly on Linux. The locked re-check reads the SQLite header instead of accepting a query that could succeed without touching the damaged file.

## [1.80.0] - 2026-07-23

### Added
- Your usage history is backed by an append-only journal at `~/.local/share/cctally/journal/`, and `stats.db` becomes a disposable index. A corrupted or deleted stats database self-heals on the next command.
- `cctally db rebuild --db stats` rebuilds the stats index from the journal on demand, reporting per-table row counts and timing. It is the manual form of the automatic self-heal.
- `cctally doctor` gains a Journal section reporting the journal's presence and writability, torn or malformed line counts, how far the stats index has fallen behind, and the most recent self-heal incident.

### Changed
- Upgrading migrates an existing install in place on first run, with no loss of irreplaceable usage history and no manual steps. Expect a slow first run while the one-time journal is written.
- Every stats write flows through a single-flight ingester, ending the `database is locked` pile-ups and wedged handles that concurrent multi-agent hook storms used to cause.
- Opening any database is faster, because schema work runs only when the schema version actually changes rather than on every open. The status line opens databases several times per render.
- Codex quota observations are preserved durably in the journal, so that history is no longer silently lost when the source rollout files are pruned.
- Back up `~/.local/share/cctally/journal/`. Hand-copying a `.db` file is now pointless rather than risky, because every database rebuilds from the journal plus surviving provider logs.

### Fixed
- The mobile dashboard hero keeps a clear gap between its 7-day and 5-hour usage values instead of letting a wide 7-day percentage overflow into the metric beside it.

### Removed
- `cctally db recover --db stats` is retired. A stats-index version mismatch self-heals by rebuild, so use `cctally db rebuild --db stats` instead. `db recover --db cache` is unchanged.

## [1.79.2] - 2026-07-23

### Fixed
- The dashboard's current-period five-hour milestone section shows the complete block position and both navigation controls immediately, matching historic periods while keeping older block details lazily loaded.

## [1.79.1] - 2026-07-22

### Fixed
- Dashboard hero history steps through actual provider reset cycles with one milestone ledger per cycle, exposes every retained 5-hour block overlapping the selected cycle, and preserves the requested block direction across lazy loading.

## [1.79.0] - 2026-07-22

### Added
- The dashboard hero modal navigates history. The week or cycle chip's `‹` and `›` step between previous 7-day billing cycles, and a block navigator scrolls the selected cycle's known 5-hour blocks, for Claude weeks and Codex cycles alike.
- A week with a re-anchoring credit renders the full ledger with cumulative cost restarting per segment and a divider between them. Historic weeks are read-only and hide the Share action.
- Every release ships to a beta channel first. Opt in with `cctally config set update.channel beta`: `cctally update` then tracks the newest of beta and stable and installs that exact version.
- Stable stays the default, flipping back to it never silently downgrades, the dashboard settings gain an Update-channel toggle, and `cctally doctor` reports the configured channel, warning on beta with Homebrew, which tracks stable only.
- Codex reasoning uses a provider-native title, summary and body treatment instead of Claude Thinking chrome. Recognized Git and memory harness markers become privacy-safe system actions with raw payload access on demand.
- Codex conversation detail preserves and renders plans, web searches, MCP completions and agent-control operations as bounded native cards with honest request, result and error state, and safe web links.
- A proven Codex spawn gains a privacy-safe link to the exact retained child conversation, only where the evidence is unique. Ambiguous, cross-root and malformed shapes stay unlinked with the generic fallback.
- Codex conversation detail exposes bounded native terminal, output and patch cards. Supported `exec` calls render as clean terminals and `apply_patch` records as faithful diff cards, while a diff-less event stays honest.

### Fixed
- The compact conversation reader keeps provider notification labels intact and the complete Codex input, output, cached-input and reasoning-output subtitle visible, without clipping or sideways overflow.
- Retained Codex world-state, inter-agent metadata and unknown future record families stay replay-safe but no longer risk becoming fabricated transcript noise or exposing private harness state.
- The dashboard's conversation reader keeps Codex reasoning, tool calls and results, injected metadata labels and lifecycle events in their native roles instead of collapsing them into generic background rows.
- A skill-invocation title no longer exposes a local `SKILL.md` path, the outline prioritizes real prompts, logical responses and compactions, and an overflow menu opened immediately after mount is no longer closed by delayed initialization.

## [1.78.0] - 2026-07-21

### Changed
- Dashboard Codex Daily, Weekly and Monthly details keep native Input, Cached input, Output, Reasoning and Total counters. Codex Weekly uses reset-cycle vocabulary and All Weekly uses neutral provider-period wording.
- Dashboard Codex session, project and quota-block details use bounded native hierarchies, localized timestamps, retained quota progression and accessible prompt expansion on desktop and mobile.
- Under `All`, a Claude project or session row opens the same detail hierarchy and cross-navigation as the Claude views, while keeping native session ids and source paths private.
- Under `All`, the Forecast and Cache Report cards compose separate provider-labelled Claude and Codex reports with their own values, confidence and unavailable reasons, instead of presenting Codex-only data under an All heading.
- Under `All`, Weekly and `$ / 1% Trend` keep Claude and Codex quota histories in separate labelled sections and series, and combine only compatible cost totals.

### Fixed
- Narrow dashboard layouts keep degraded source status readable, hide the Codex five-hour slot when no native window is reported, and label real All-mode five-hour rows by provider.
- Source-wide freshness and read-model warnings reach Daily and Projects without hiding the fixed ten-card board. Current Usage and Share stay bound to the source that opened them.
- Dashboard HTTP and live-event JSON convert a non-finite number to `null` at one outbound boundary, so a missing doctor freshness marker no longer emits a browser-invalid `Infinity` token and the full doctor report opens.
- Claude 5-hour and 7-day usage receive one authoritative confirmation per 30-second status-line cycle, so a stale `rate_limits` payload can no longer leave dashboard milestones up to five minutes behind.

## [1.77.0] - 2026-07-21

### Added
- The dashboard Conversations workspace browses and reads Claude and Codex conversations through one reader, with a `Claude | Codex | All` rail selector, collision-safe permalinks and Codex-native rendering.

### Changed
- Conversation prose, normalized Codex events, browse rollups and full-text indexes live in their own `conversations.db`. A large, rebuilding, locked or missing transcript store no longer delays dashboard accounting freshness.

### Fixed
- Conversation routes work on Python and SQLite builds that would otherwise treat the read-only database URI as a literal path.

## [1.76.0] - 2026-07-20

### Added
- `cctally doctor` reports `db.reclaimable` when at least 25% of `cache.db` pages are free, with a direct `cctally db vacuum --db cache` remedy.

### Fixed
- Codex dashboard Weekly cycles and `$ / 1%` accounting keep the separately metered GPT-5.3-Codex-Spark pool distinct from the standard seven-day pool, so a brief Spark session no longer creates a phantom standard week.
- Dashboard Codex Weekly cost deltas no longer render 100× too large, and the Codex `$/1% Trend` table aligns Used%, `$/1%` and vs-prior values under their matching columns again.
- Claude dashboard quota keeps updating while Claude Code uses a bracketed context variant such as `opus[1m]`. Context-window metadata no longer suppresses the valid account-wide 5-hour and 7-day observations.
- Conversation-retention pruning commits each session separately while holding its locks, bounding the write-ahead log during a first prune. A 9.37 GB benchmark fell from 889.7 MiB of log to 28.8 MiB with identical results.

## [1.75.1] - 2026-07-20

### Fixed
- Database repair rejects an SQLite shell without working `.recover` support before creating a forensic copy, and explains the capability it needs.
- Automatic transcript-space reclamation runs in bounded, progress-checked chunks, fixing the Python 3.11 and Linux case that could leave freed pages on the freelist after a retention prune.

## [1.75.0] - 2026-07-20

### Added
- `cctally codex percent-breakdown` is a native seven-day Codex milestone command with the same terminal design as `cctally percent-breakdown`, current or retained cycle selection, and matching five-hour context.
- Guided `stats.db` corruption repair and SQLite-native online backups, with verified recovery, preserved usage history, safe retention of the original file, and actionable malformed-database errors.

### Fixed
- Conversation-transcript retention reclaims disk automatically and defaults to a tighter window, so `cache.db` no longer grows to gigabytes of transcript text that slow every dashboard tick.
- A freshly created `cache.db` uses incremental auto-vacuum and the daily prune returns freed pages to the operating system. An older `cache.db` keeps the legacy behaviour until one `cctally db vacuum` or `cache-sync --rebuild`.
- The default `conversation.retention_days` is now 90, down from 180. Set your own window, or `off`, with `cctally config set conversation.retention_days`.
- The dashboard Codex hero uses the exact Claude hero composition and metric order over its native seven-day cycle. Unavailable cycle accounting stays explicit instead of falling back to token or budget tiles.
- Force-refresh and automatic OAuth refresh serialize their 429 backoff under one lock, so a completed force-refresh can no longer erase a newer automatic-refresh cooldown.
- Dashboard Codex modals populate the canonical current-cycle, session, period, project, cache, forecast, alert and `$/1% Trend` views from native data instead of sparse placeholders or renamed semantics.
- The current 5-hour block no longer lingers as an approximate `~` window in `cctally blocks` and the dashboard while the status line already shows the correct data. The dedup self-heal now materializes the rolled-over window's anchor.

## [1.74.0] - 2026-07-19

### Fixed
- Dashboard Daily, Weekly and Monthly details use the same bounded history across Claude, Codex and All — 30 days, 12 weeks and 8 months — with compact date labels and contained mobile tables.
- Dashboard Trend, Projects, Cache Report, Forecast and Alerts details keep their canonical structure for Codex and All, showing provider-native values or an explicit unavailable reason in every slot.
- Source-qualified Session and Project details preserve Codex accounting totals when project metadata is incomplete, and every source detail shares the standard focus, Escape, return-focus and scroll-lock lifecycle.
- Codex accounting no longer double-counts parent token events copied into a forked or subagent rollout before its first model context. A cache migration replays retained rollouts to remove existing duplicates.
- Because of that, `cctally pricing-check` and `doctor` no longer report those ingest artifacts as a missing embedded price, while a real unknown model id stays actionable.
- Dashboard Codex Weekly rows follow observed native seven-day re-anchors instead of calendar weeks, collapsing reset jitter. Codex forecasts use elapsed native-cycle pace rather than activity bursts pinned at 100%.
- Dashboard Codex source reads keep accounting, quota, budget, session and forensics panels when retained rows still lack project attribution. The source is fresh-but-partial and disables Projects only.
- The dashboard Codex hero's cost and token counters cover the active native seven-day reset cycle rather than an unrelated calendar slice. A missing boundary leaves quota and budget visible and marks the hero unavailable.

## [1.73.0] - 2026-07-18

### Added
- The dashboard's twelve conversation routes accept a Codex `v1.` key alongside a Claude session id, and the three collection routes take a strict `?source={claude,codex}`. The Claude routes stay byte-identical without it.
- `cctally transcript export` accepts a Claude `sessionId` or a `v1.` conversation key, anonymizes qualified exports by default with `--raw` to escape, and adds a Codex-only `--speed {auto,standard,fast}`.
- `cctally transcript search --source {claude,codex}` searches either provider. The Codex output is its own `Key / When / Project / Kinds / Snippet` table with a camelCase JSON envelope paginated by an opaque `--cursor`.
- Codex conversation live-tail streams a watched conversation's events, and a budgeted discovery pass joins child threads spawned mid-watch, so each tick's cost stays proportional to the conversation you are watching.

### Fixed
- Status-line usage persistence arbitrates concurrent Claude sessions instead of accepting the first lock winner, so a fresher 5-hour or 7-day observation is selected regardless of render arrival order.
- The internal 25-second persistence throttle is replaced by that arbitration. The existing `statusLine.refreshInterval: 30` remains the idle-time trigger, and `cctally doctor` reports pipeline evidence when repair is needed.

## [1.72.0] - 2026-07-17

### Fixed
- `bin/_lib_conversation_retention.py`, the transcript-retention prune engine shipped in 1.71.0, was missing from the npm package, so an installed `cache-sync --prune-conversations` and the dashboard's background prune crashed at import.

## [1.71.0] - 2026-07-17

### Added
- The dashboard's background sync prunes conversation transcripts older than the new `conversation.retention_days` key at most once a day, bounding `cache.db` growth. Only transcript rows are pruned, and all of them are re-derivable.
- `cctally cache-sync --prune-conversations` prunes transcripts past the retention window on demand and reports the rows removed per provider.
- `cctally db vacuum [--db {cache,stats,all}]` reclaims the disk space a prune freed. It is never automatic, runs under an exclusive lock, and refuses when free disk is below about twice the file size.

### Fixed
- `cctally dashboard` no longer pegs a CPU core under sustained use on a large cache. The Codex quota reconcile that ran on every tick now short-circuits when the Codex state is unchanged, and the sync thread caps its duty at half a core.
- The source selector no longer makes the mobile topbar scroll sideways on the narrowest phones. The status chip hides at 360px and below, and both it and the selector drop out of the condensed header once you scroll.

## [1.70.0] - 2026-07-17

### Added
- `cctally setup` adds `statusLine.refreshInterval: 30` to a cctally-pointing Claude Code `statusLine` block that lacks one, so usage keeps recording on a timer while a session waits on a long-running subagent.
- Ownership of that key is add-when-absent and never mutate: setup never creates a `statusLine` block, never changes a `refreshInterval` you set yourself, and `--uninstall` leaves it alone.
- `cctally doctor` gains a `hooks.statusline_refresh_interval` check that warns when a recognized cctally `statusLine` command is missing its `refreshInterval`.

### Changed
- The status-line usage-persist throttle moves from 60 s to 25 s so it sits below the new 30 s refresh timer. A 60/60 pairing throttled every other tick and oscillated the effective cadence between 60 and 120 seconds.

### Fixed
- Status-line usage persistence skips a session running a bracketed model variant such as `claude-opus-4-8[1m]`, whose limits describe a separate usage pool. Persisting those froze every later genuine write.

## [1.69.3] - 2026-07-17

### Fixed
- The persistent `⚠ sync error` chip that appeared once a provider stayed degraded across two or more sync ticks is gone. A source with no coherent prior generation now stays unavailable and carries its warning.

## [1.69.2] - 2026-07-17

### Fixed
- Four source-aware runtime modules were still missing from the npm package after the 1.69.1 fix, so installed commands kept crashing at import. They now ship. Homebrew was unaffected, because it archives the whole tree.

## [1.69.1] - 2026-07-17

### Fixed
- The 1.69.0 package was missing four runtime modules that the source-aware dashboard and CLI analytics load at startup, so every installed command crashed at import.

## [1.69.0] - 2026-07-17

### Added
- A Claude/Codex/All selector in the dashboard header, cycled with `v`, drives every panel, modal, alert filter and share default at once, rendering each provider's own vocabulary and hiding panels a source does not publish.
- Alerts and toasts read the per-source projections, so a Codex budget alert cannot double-toast, and the alert settings regroup into global, Claude and Codex controls. A per-source status chip carries freshness and degraded warnings.
- Every share render, composer section, preset and history row carries the source it was captured under. Switching the selector mid-flow never restamps an open share, and `all` composes provider-labelled sections.
- `project`, `diff`, `range-cost`, `cache-report` and `report` take `--source` to choose Claude, Codex or separate all-source sections, or use the fixed `cctally claude|codex` subgroup forms.
- Codex projects use privacy-safe qualified identities, and its cache surface reports truthful token reuse rather than a fabricated cache-hit rate. The Codex daily, monthly, weekly and session reports gain `--config PATH` and the share flags.
- `cctally codex quota` history, statusline, forecast, blocks and breakdown interpret locally retained Codex quota windows without combining roots or limits, with optional setup-managed Codex hooks and opt-in quota alerts.
- The status line is now the primary automatic writer of weekly and 5-hour usage, so usage stays fresh from live sessions even while Anthropic's usage endpoint is rate-limiting the background poll.

### Changed
- The background usage poll is a backfill behind the status line. It fetches only when the status line has not fed recently, honours `Retry-After` and backs off exponentially on `429`, so a transient rate limit cannot snowball.
- The dashboard Sessions table is keyboard-navigable as a grid: the whole body is one Tab stop, Up and Down move the active row, Enter opens its detail, and Left and Right reach the per-row controls. It replaced about 400 tab stops.
- The dashboard board is responsive across three width modes: below 900px it stacks, 900–1199px shows Sessions full-width with Trend and Projects paired, and 1200px and up keeps the dense three-across layout.
- Touch targets on the dashboard board — topbar actions, sort headers, expand and collapse, model chips, the drag grip — meet the 44px floor on any coarse-pointer device, including tablets.
- The unscrolled phone hero is tighter, so the start of the board is visible above the fold on short phones, and each session row is openable by keyboard and touch through its title rather than by mouse only.
- Below 900px the Weekly and Monthly cards preview the three most recent periods with a "+N more" button to the full table, and the Blocks card scrolls within a bounded height. Layouts at 900px and up are unchanged.
- Dashboard cards describe rather than impersonate buttons: activating a card's Share or Expand control no longer also opens the card's own modal, and Tab skips the card body straight to its actions.
- The 44px touch-target floor extends off the board, to modal close buttons and sort headers and to every Share and Composer control, on phones and tablets alike.
- The phone Share form no longer zooms on focus and shows a live preview above the options, so edits are visible as you make them. The Composer's controls are touch-sized.
- A non-empty Composer basket stays reachable in the condensed mobile header after you scroll, and the topbar no longer overflows sideways on a 320px phone.
- The conversation browse rail renders far faster on large caches. Per-session enrichment — git branch, models, title, cost — is materialized at ingest instead of re-read per request, and the rollup cost refreshes on a pricing change.
- Dashboard detail surfaces, the conversation reader's body and the browse rail stop re-fetching on every five-second tick. They revalidate only when the underlying data changes, so a finished session or conversation is fetched once.
- The desktop conversation reader header is regrouped into reading, navigation, sharing and bulk clusters with a quiet status cluster, and it folds to the compact controls when the outline column squeezes the reader below 720px.
- Conversation rail rows split their metadata into an identity line and a stats line. Model chips render at the 11px typography floor, and a long unknown-model name is bounded so it cannot push cost or message counts off the rail.
- All sub-floor viewer text is raised to the 11px floor and guarded by a static scan, and the overflow menu's completion row is an actionable jump at compact and folded widths.

### Fixed
- Usage snapshots record the true source of each write, `statusline` or `api`. Rows fed by the usage endpoint were previously mislabelled `statusline`.
- The dashboard Sessions Cost column stays visible at every width instead of hiding behind a horizontal scrollbar. The card folds Duration into the start time and sheds Cache, then Project, as it narrows.
- Dragging to reorder a tall dashboard card no longer stretches or squashes the card it displaces.
- The decorative swipe handle is gone from the top of dashboard modals. It advertised a swipe-to-dismiss gesture that was never wired up; dismissal stays Escape, backdrop tap or the × button.
- The conversation browse filter popover and a model-filtered rail no longer do a 22-second cold full-table scan on large caches. A new partial index makes facets and model filtering index-only.
- The 640/641px responsive cliff in the conversation viewer is fixed. The workspace stays single-pane until about 880px, and the compact reader header applies across the whole constrained band rather than only on phones.
- Conversation comparison is fully operable at compact widths. `⋯ → Compare with…` opens the rail picker, Cancel and Escape return to the anchor reader with focus restored, and Run A and Run B identity is spoken with the header titles.
- A startup race where the first transcript-gated request could error out the instant the port opened is fixed. Module loading is serialized, so every request sees a fully loaded module.
- Each model's stacked-bar segment in the Blocks panel paints the same colour as its legend dot and its model chip. The gauge drew from a generic palette, which swapped green and blue for two of the models.

## [1.68.0] - 2026-07-13

### Added
- `cctally db checkpoint [--db {cache,stats}] [--json]` drains a bloated write-ahead log quickly and non-destructively. It is the manual escape hatch and the `doctor` remedy for that state.

### Changed
- `cctally doctor` gains a read-only `cache.db WAL size` check that warns when the log exceeds 256 MB and points at `cctally db checkpoint`.

### Fixed
- The `cache.db` write-ahead log no longer bloats to gigabytes under concurrent multi-agent syncs, which surfaced as `Error: database is locked` on every command. A bloated cache self-heals on the next sync, or run `cctally db checkpoint`.

## [1.67.1] - 2026-07-12

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.67.0] - 2026-07-12

### Added
- The transcript reader gains an `Anon` toggle beside Export, on by default and remembered, that rewrites your project paths, home directory and username to placeholders and redacts known secret patterns before an export leaves your machine.
- `--raw`, or turning the toggle off, restores the exact unredacted output byte for byte. Per-card copy is fail-closed, so a scrub failure leaves the clipboard untouched. It is best-effort, so review before sharing.
- `cctally transcript export <session-id>` prints a whole session as Markdown, anonymized by default and byte-identical to the dashboard download. `cctally transcript search <query>` runs the cross-session search from the terminal.
- `cctally setup`'s first-run cache sync shows live progress: a pre-sync notice, per-file counts and a heartbeat, so a large history no longer looks hung. Progress is stderr-only, so `--json` and piped runs are unaffected.

### Changed
- `cctally setup` ends with the restart step as a prominent `▶ Next step:` call to action instead of burying it mid-output. The README also clarifies that only auto-recording needs `cctally setup`.
- `forecast` and `diff` apply the reset-aware floor to a credited historical week's usage percentage, matching every other used-percent surface. Uncredited weeks are unchanged.
- In the conversation reader, Escape steps back one level — reader, then conversations list, then dashboard — instead of ejecting straight to the dashboard. An open outline sheet is peeled first.
- `cctally tui` is documented as being in bugfix-only maintenance mode. Both layout variants keep working and stay supported, but the terminal UI no longer tracks new dashboard features.

### Fixed
- The Blocks panel's per-block cost bar renders the fable model in its own rose colour instead of the default grey. The gauge segment was missing its colour rule while the other models had theirs.
- 5-hour block start and window times round to the nearest 10-minute boundary everywhere they are displayed, so a reset captured as `10:39` shows as `10:40`. The stored timestamps stay exact, so lookups are unaffected.
- A find-jump into a message inside a collapsed subagent thread no longer yanks the reader to the bottom. The reader disables the resize-autoscroll watcher while a jump is navigating.
- `transcript export` no longer cold-starts slowly on a very large cache. The lookup used for anonymization is backed by a partial index, turning a full-table scan into an index-only lookup.
- A single-page conversation opens at the top again. A multi-page tail open still lands at the bottom with live-tail engaged.
- A cold-tail outline or find jump lands reliably. Jumping to an earlier message from the bottom of a long conversation waits for the loaded window to catch up before deciding a target is unreachable.
- The README's privacy tagline discloses the anonymous, opt-out install-count beat instead of denying it, and the absolute claim is qualified to "cctally never uploads your session data". The license line is corrected to Apache-2.0.
- The architecture and runtime-data documentation is rewritten to describe the real module layout, the migration framework and the full stats schema, and the three block-related command pages now cross-reference each other.

## [1.66.0] - 2026-07-10

### Added
- Reporting `--json` output carries a `schemaVersion: 1` key, rendered first, so a consumer can version-gate against the payload shape. It is added to every reporting surface that lacked it, including empty and error forms.
- The cache sync counts JSONL lines it cannot parse or has to skip, per provider, and surfaces them on the `cache-sync` output, the hook log line and a new `cctally doctor` ingest parse-health check.
- `CCTALLY_DEBUG=1` prints the full Python traceback on stderr when a command crashes, and `CCTALLY_DEBUG_LOG=<path>` appends the backend log to a file, so a bug is reportable without editing source. Default output is unchanged.
- The dashboard logs server errors to the terminal running it instead of swallowing them silently. Routine client errors and the per-request access log stay quiet.
- `cctally doctor` gains a SQLite integrity check, run only from the CLI where it is affordable, and an informational check reporting whether a sync lock is currently held.
- `CCTALLY_PERF_TRACE=1 cctally cache-sync` traces both the Claude and Codex ingest under one shared phase tree, with the Codex sync carrying the same timing seams as the Claude sync.
- A new unique constraint on the Claude session cache means an offset-bookkeeping regression can never silently double-count your cost data. A collision fails loudly and rolls back that file, and pre-existing duplicates are removed.
- `cctally pricing-check` flags embedded-pricing suppressions that no longer match a real divergence or are past their stated cutover date, so a deliberate pricing override cannot silently ossify.
- Codex pricing covers the `gpt-5.6` family, so cost for those models is computed from their own published rates instead of falling back to `gpt-5` pricing.

### Changed
- `cctally project` and `cctally forecast` exit 2 rather than 1 on their own usage errors, matching the rest of the cctally-native family. The parity commands keep exit 1 on a bad date, and the convention is documented.
- `cctally report --json`'s `generatedAt` routes through the same clock as the rest of the report, which makes the output reproducible under a pinned clock. In normal use it still reflects wall-clock time.

### Fixed
- `cctally setup` installs the `cctally-budget` symlink, which existed but was never linked.
- `CCTALLY_ALLOW_PROD_MIGRATION=0`, `CCTALLY_DISABLE_DEV_AUTODETECT=0`, `CCTALLY_DEBUG=0` and `CCTALLY_DISABLE_UPDATE_CHECK=0` mean disabled. Previously any value, including `0`, enabled these flags.
- A corrupt `stats.db` produces a one-line diagnosis with recovery guidance and exit 2 instead of a raw traceback.
- Piping output to a closed reader, as in `cctally daily | head`, exits 0 quietly instead of printing `Error: [Errno 32] Broken pipe`.
- The dashboard share endpoints cap request bodies at 64 KiB and the server sets a 60-second socket timeout. A live-event stream treats a send timeout as a normal client disconnect.
- The dashboard's `/api/data` returns a JSON 500 on an internal error instead of dropping the connection with no response, and a live-event stream that fails mid-flight is logged and closed cleanly.
- `cache.db` opens with `PRAGMA synchronous=NORMAL`, which means fewer fsyncs during ingest on a fully re-derivable database.
- Codex resume tracking persists the ingest iterator's actual watermark rather than a reconstructed sum, closing a latent double-count on rollouts whose cumulative and per-turn accounting diverge. Healthy sessions need no re-ingest.
- The direct-JSONL fallback parses with the same implementation as the cache path, so the two cannot drift, and a malformed cost value in a session line degrades to the token-derived cost instead of aborting the read.

## [1.65.0] - 2026-07-09

### Added
- Opt-in backend performance instrumentation. `CCTALLY_PERF_TRACE=1` prints a phase-timing trace to stderr on `cache-sync`, and the dashboard's loopback-only debug endpoint reports live backend timings and cache state.
- That trace breaks whole-session conversation assembly into named sub-phases, and the debug endpoint can surface a conversation-open trace, so a slow transcript open is attributable to a stage on a live dashboard.
- The conversation browse rail and cross-session search can be filtered by model family. A new Model section in the filter popover lists the families that actually appear, each with a session count, with removable chips and Clear all.

### Changed
- The conversation reader no longer re-processes an open conversation on every background refresh. It recomputes only when the conversation actually grows, which cuts idle CPU and battery use for anyone leaving a transcript open.
- The dashboard opens instantly on a heavy-history instance. It binds and paints the current-week and forecast panels almost immediately, and the heavier panels hydrate over the live stream with a brief loading skeleton.
- A first-run or long-gap dashboard fills in progressively as history is ingested, instead of sitting blank and then snapping to loaded. A slow or backgrounded tab always converges to the latest data rather than sticking on a stale frame.

### Fixed
- The conversation filter popover retries once if its Project and Model lists fail to load on a transient hiccup, instead of coming up empty and staying that way until it is reopened.

## [1.64.0] - 2026-07-07

### Added
- cctally sends an anonymous, opt-out install-count beat at most once a day. It transmits a one-way token that rotates monthly, the version and a coarse operating-system family — no IP, username, paths or usage data.
- Nothing leaves your machine for at least 24 hours after first run, so you always have a window to opt out. `cctally telemetry` shows the current state and exactly what would be sent, and `cctally telemetry off` disables it.
- It is also disabled by `cctally config set telemetry.enabled false`, `CCTALLY_DISABLE_TELEMETRY=1`, the `DO_NOT_TRACK` convention, and automatically in a development checkout. `docs/telemetry.md` documents the token.

## [1.63.0] - 2026-07-07

### Added
- `cctally statusline --usage-only` renders just the subscription usage chip, `5h X% · 7d Y%`, dropping the model, cost, burn-rate, context and countdown segments. Contributed by @nathanm4.

## [1.62.0] - 2026-07-06

### Changed
- Dashboard warm rebuilds are materially faster on very large histories. The Daily, Weekly, Monthly and Projects panels fold only new activity into the current period instead of re-summing it. Output is unchanged.
- The cache-report card joins that fast path too: each closed day's breakdown is computed once and reused, so a live refresh recomputes only today. A quiet day is remembered as quiet rather than forcing the whole window to be re-read.

### Fixed
- The live dashboard no longer serves a stale value for a past day, week or month after a streaming message that began in that period finalizes once the period has rolled over. The affected period is recomputed.
- The live dashboard's memory footprint no longer creeps upward for each day it stays running. The cache-report card's per-day cache drops days that have rolled out of its trailing two-week window.

## [1.61.0] - 2026-07-05

### Added
- `cache-sync --prune-orphans` cleans up cache rows left behind when a session's transcript directory is removed, for example after you delete a git worktree. It only removes rows it can prove are safe to drop.

### Changed
- The dashboard's 5-hour Blocks card is a full-size half-width card the same size as Weekly and Monthly, instead of a cramped third-width tile. Forecast moves up beside it, and Recent Alerts spans the full width of the bottom row.
- Warm dashboard refreshes are much faster on large histories with many resets. A closed subscription week's totals never change, so the Trend, Forecast and Projects panels compute each closed week once and reuse it.
- The Projects panel also resolves each distinct session path once rather than once per session, so on the largest histories its rebuild drops from seconds to a fraction of a second while staying byte-for-byte identical.

### Fixed
- The live dashboard no longer pegs a CPU core after long uptime or on a large history. An idle dashboard sits near 0% CPU, and when new usage lands it recomputes only the current day, week and month rather than your whole history.
- The dashboard cleans up cache rows left by removed session directories on its own, once at startup and periodically while running, so the `no longer on disk` warning no longer repeats every few seconds.
- `cache-sync --rebuild` waits up to 30 seconds for the cache lock and exits non-zero if it still cannot take it, instead of silently doing nothing and reporting success while a dashboard is syncing.

## [1.60.0] - 2026-07-03

### Changed
- The dashboard board is a content-aware bento grid. Cards sit in height-matched rows sized to their content and the layout goes full-width up to 2100px, roughly halving the page height, while still collapsing to a single column on mobile.
- The hero is rebuilt into three zones so each question you ask gets a dominant number: weekly usage percent with 5-hour usage and the reset countdown beside it, and dollars spent this week with `$/1%` as its sub-line.
- A support column carries the end-of-week forecast, the week-over-week `$/1%` trend and a snapshot-freshness reading that stays calm for a normal few-minute-old snapshot and turns amber, then red, only once the data is genuinely stale.
- Every card gains a consistent ⤢ expand control in its header, the Forecast card gains a pace bar toward its 100% cap, and the empty Alerts card shows a gauge of your current usage against the configured thresholds.
- Recent Sessions is denser. Where every recent session uses the same model, the per-row model column is dropped and a single caption says it instead. A Session title and a Cache hit-rate column take its place.
- That title is private conversation content, so it appears only where transcript viewing is enabled for how you reached the dashboard, and shows a dash otherwise. A long title shortens with the full text on hover.
- The `$/1% Trend` detail view opens in a two-pane layout and gains a sortable Cost column. With too little history for a four-week median, the two empty tiles collapse into a single "needs 4 weeks" hint.
- Daily, Weekly and Monthly are three independent cards again, each with its own detail view, header and keyboard shortcut, undoing an earlier consolidation that hid Weekly and Monthly behind an easy-to-miss toggle.
- A card whose content is taller than its row scrolls inside its own frame instead of rendering on top of the card below it, the Blocks card shows every block for the week, and the Daily heatmap shows each day's dollar amount again.
- On a phone the Recent Sessions title is no longer crushed to nothing, each session leads with what it was about, the hero is a compact card rather than three tall zones, and the Daily card title no longer clips.

### Fixed
- The empty Recent Alerts card shows the same "you're clear" gauge as the alerts detail view, centred and sized to the card, instead of sitting bottom-heavy under a band of empty space.
- The `$/1% Trend` card keeps its sparkline and legend pinned in view and scrolls the weekly table beneath them, so with only a few weeks of history the chart is no longer half-hidden below the card's scroll fold.
- The Blocks card's expand control is disabled on a week with no activity blocks yet, instead of looking clickable and opening nothing.
- The Weekly and Monthly cards list every week and month and scroll within the card, instead of showing only the three most recent behind a scrollbar that revealed nothing more.
- The Daily heatmap no longer clips the top off each day-of-month number. The card row is slightly taller, so every day's number and its dollar amount are fully legible.

## [1.59.0] - 2026-07-02

### Changed
- Dashboard detail modals scale their charts to the real data. The Cache Report hit-rate timeline auto-zooms its y-axis to the data band, so a line clustered at 96–98% shows real variation instead of pinning flat to the top.
- The Projects chart switches to a ranked top-N and "(other)" bar view when one project dominates the window, and the stacked-area mode gains y-axis labels. Each bar still drills into that project.
- The Forecast range bar gets outlined zones, a 0-to-110% scale, a "now" marker at your current weekly usage, and a legend. The Trend chart's used-percent line gains its own right-hand axis and hover and keyboard tooltips.
- A single-model session collapses its redundant Models and Cost-by-model sections into one line, and every panel modal lands keyboard and screen-reader focus on its heading when it opens.
- Every human-facing date and timestamp renders in one style. The Session modal and the freshness tooltip localize to your display timezone instead of leaking a raw UTC stamp, and every Cache Report date reads `Jun 29`.
- The Trend view states a week count derived from the data instead of a hardcoded one, and its median line names its own window, so it is no longer confused with the hero's separate four-week median.
- The Projects table renames its two percent columns to say what they measure, `Used pp` and `Cost share`, each with a tooltip. The active 5-hour block reads `191 min left` rather than `191M LEFT`, which looked like millions.
- The Settings overlay is one predictable deferred-commit form. Every edit is staged with a `Save · N changes` badge, and the Reset buttons no longer silently discard your other pending edits.
- Settings groups Alerts into Threshold, Budget and Test, consolidates three scattered Reset buttons into one Restore defaults section with explicit scopes, and asks before discarding unsaved changes on Escape or a backdrop click.
- The Settings and Help icons appear in the desktop header rather than being reachable only by keyboard, and the sync-freshness indicator reads `synced 8m ago` and escalates colour as it ages.
- In Recent Sessions, search highlights the matched text in the cell and marks the current match distinctly, and on a phone the search box takes its own full-width row so a filter chip cannot crush it.
- Sortable table headers show a dim glyph at rest so they look sortable before you click, the Doctor and Basket chips meet the 44px touch target, and the Weekly, Monthly and Projects modal rows are keyboard-selectable.
- Freshness readouts speak plain language everywhere. The hero's "as of" pill and the doctor report's snapshot line read `1d 3h ago` or `27m ago` instead of a raw seconds count.
- The Session and 5-hour Block detail modals present their per-model cost breakdown in the same one-row-per-model format the history cards use — a colour-coded chip, a bar and the dollar cost — replacing two one-off stacked bars.

### Fixed
- The Settings overlay no longer overwrites a pending edit when the same field changes in another tab or through `cctally config set`. An incoming value is adopted only for fields you have not touched since the last sync.
- The Forecast modal's two projection pills no longer overlap at narrow widths, collapsing to a single range pill only when they genuinely cannot fit, and the mobile scale labels no longer crowd into `100%110%`.
- On a phone the Projects ranked-bar view wraps each bar's label to the full project name instead of truncating it, because there is no hover tooltip on touch.
- The Forecast range bar reflows its pills immediately when the modal is resized instead of waiting for the next refresh, and the empty Alerts gauge gives interior thresholds a distinct middle tone.
- The History modal's Day, Week and Month toggle is a proper keyboard radio group: Tab lands on the selected period, and the arrow keys move focus and selection with wraparound.
- Cost-by-model chips use one style everywhere the breakdown appears, and the Projects drill shows a short one-line model name such as `opus-4-8` instead of the full canonical id that wrapped to a second line.

## [1.58.0] - 2026-06-30

### Added
- Embedded pricing for Claude Sonnet 5, so its sessions are costed instead of falling back to $0 as an unknown model. The rate is the standard $3/$15 per MTok, flat across the full 1M-token context window.

### Changed
- The dashboard's at-a-glance redesign makes weekly Used % a full-width hero strip flanked by four spelled-out metrics and a freshness chip, replacing the five header stats and the Current Week grid card.
- The panel grid is two-tier: uniform-height summary tiles above full-width data cards, with drag-and-keyboard reorder that cannot cross tiers. That removes the old grid's dead space and roughly halves the page height.
- On a phone the hero stacks with a 2×2 metric grid, and the sticky top bar collapses to one short row that reveals a condensed usage and reset readout once you scroll past the hero.
- Dashboard cards wear a calm, neutral chrome. Borders, hover elevation, header dividers, the focus ring and every panel title read in one neutral treatment instead of eleven competing accent colours.
- Accent colour is reserved for genuine state: the Cache Report header and the Forecast verdict keep their meaningful hues while healthy cards stay quiet. Recent Sessions caps its height and scrolls with its headers pinned.
- The Weekly, Monthly and Blocks model-split bars gain a compact inline legend — a model dot, a short name and a percentage, with a `+N` overflow — so the breakdown is legible on touch instead of hiding in a tooltip.
- The panel drag-to-reorder grip is visible at rest on desktop and brightens on hover, the footer keyboard-hint strip is hidden on touch devices, and the Current Week gauge no longer crowds its milestone ticks below 15% used.

### Fixed
- The Projects panel on a phone no longer clips a four-figure project cost. The cost column is content-sized, so a value like $1,234.56 renders in full and the project name ellipsizes first.
- The Forecast panel's verdict chip shows the glyph that matches its state — a tick when healthy, a warning on warn, a stop when over — instead of always painting a warning triangle.
- Dashboard form controls on a phone no longer trigger iOS Safari's auto-zoom on focus, and the smallest primary data text in Sessions, Projects and Trend is raised to a legible 14px floor.
- The Current Week card no longer shows the snapshot freshness twice. The duplicate footer is gone and one header chip is the single source of truth, calm when fresh and amber only when genuinely stale.

### Documentation
- The README is rewritten as a feature-led landing page, with a per-capability tour replacing the prior bullet list, a conversation-viewer showcase and two new screenshots, all regenerated against the current models.

## [1.57.1] - 2026-06-28

### Fixed
- In the conversation viewer, a tool call that follows a checklist update inside the same assistant turn is no longer silently dropped. Only the checklist card rendered, and every tool call after it vanished.

## [1.57.0] - 2026-06-27

### Added
- Codex tool calls in the conversation viewer render as a dedicated card. The prompt and response render as Markdown, where the response was previously a raw JSON blob, with a header showing model, reasoning effort and sandbox.

### Fixed
- Model chips no longer mislabel or miscolour Fable and unrecognized models. A `claude-fable-5` conversation rendered a rail chip literally labelled "sonnet", and any unrecognized model borrowed that identity everywhere.
- Fable gets its own rose chip on every surface, and a genuinely unrecognized model gets a neutral grey "other" chip labelled by its own abbreviation instead of impersonating sonnet.

## [1.56.2] - 2026-06-27

### Fixed
- The conversation browse rail's model chip is no longer truncated mid-glyph by the cost and message count beside it. The rail shows one rigid primary-model chip plus a `+N` counter, and the project text ellipsizes first.
- A conversation whose main session ran one model but spawned subagents on another lists the main model first, as `opus, haiku` rather than alphabetically, which previously made a haiku subagent look like the main model.

## [1.56.1] - 2026-06-27

### Fixed
- A large subagent thread renders an internally windowed slice centred on the turn you asked for, with "Show N earlier" and "Show all" controls, so a deep link into a giant subagent no longer mounts a 106,000-node page.
- A deep-linked turn re-lands after a browser reload. The viewer opts out of the browser's native scroll restoration, so the deep link is the only thing positioning the viewport.
- The comparison view's A→B metrics strip stays un-clipped at every width. It reflows on its own width to three columns, then two, then a single full-width column, rather than on the viewport, which could not see the rail.

## [1.56.0] - 2026-06-26

### Fixed
- The conversation reader's header toolbar no longer overflows or hides the Compare, Find, outline, Latest and Export controls at laptop and tablet widths. It wraps onto its own full-width row at all sizes.
- The reader header's overflow, export and focus menus close when you click outside them, not only on Escape or a focus change.
- A long subagent-card title is recoverable in full through a hover tooltip.
- The current in-conversation find match is visually distinct from the other matches while you step through results.
- The discovery rail's section headers are sort-aware: Recent and Oldest keep date dividers, Cost and Messages show a flat list, and Project sort groups by project name. The Filters and Sort controls meet a 44px tap target on mobile.
- Pressing Escape in an open comparison closes it back to the single reader and restores focus to "Compare with…", instead of ejecting the whole workspace to the dashboard.

## [1.55.0] - 2026-06-25

### Added
- An agent or subagent thread keeps a sticky orientation header and a tinted body wash, so you always know which agent you are reading at any scroll depth. The pinned header doubles as an always-reachable collapse control.

### Changed
- The conversation reader virtualizes its message list, so only the cards in or near the viewport are mounted. A cold deep link into a 1,000-turn session is interactive within a beat and stays smooth as you scroll.
- The cross-session search input takes a full-width row with a leading magnifier, and Filters and Sort move to a second row beneath it. Browse rows collapse to one scannable line with a compact model chip and a right-aligned cost cluster.
- The open conversation is highlighted with a left accent bar in the browse list and in search results, and a zero-result search shows a richer empty state that echoes the query with a "Search all conversations" escape.
- The in-conversation find bar gains an always-visible `regex` and `case` mode tag and a stronger pressed state, reflowing on a phone to a full-width input with the toggles, counter and navigation wrapped beneath.
- In-conversation find scrolls the matched word itself into the centre rather than centring the whole turn, and highlights matches inside fenced code blocks and tool panels that previously showed no highlight at all.
- In the comparison view a metric regression shows a red ▲ rather than a neutral grey one, A and B identity is anchored with a blue and purple accent throughout, and diff rows are marked `−`, `+` or `=` so colour is not the only cue.
- An agent spawned from a main turn sits on the main thread spine with a magenta branch dot instead of being indented like a nested agent. A `↳ launched <kind> agent` connector marks where each subagent was launched.
- An expanded thinking block gets a quiet-reasoning treatment — an indigo left border and wash, with the prose dimmed, italicized and one size smaller — so reasoning no longer reads like the assistant's actual answer.
- The per-turn cost footer is quieter. The micro-bar stays on every assistant turn, but the verbose token text renders only at or above $0.05; below that the exact figure moves into the footer's tooltip.
- On a phone the reader header collapses to two slim rows, so reading starts at the top of the screen. Export, Compare, Latest and the expand-all controls fold into an overflow menu, and the focus segment becomes one compact dropdown.
- Every interactive element in the conversation viewer shows the same blue keyboard-focus ring, replacing a mix of border, outline and inset-shadow styles, and the viewer's spacing is put onto a shared 8px scale.

### Fixed
- Pressing Escape in the conversation search box clears and unfocuses it instead of also falling through to the global Escape and ejecting you to the dashboard. Escape with the filters popover open closes the popover.
- A find hit inside an auto-expanded thinking or tool block lands the matched word dead centre on a tall turn. The block settles to a shorter height after the scroll committed, which drifted the word upward by up to a few hundred pixels.
- A cold far jump lands on its target on the first click. The reader walks the list toward the target so the intervening rows measure, then writes the scroll position directly to the now-known offset.
- A find hit deep inside a collapsed subagent card force-opens its enclosing card, reveals the match highlight and centres the turn, instead of recentring the card itself with no visible highlight.
- Jumping to an already-loaded far-off turn lands precisely on it. The jump uses a convergent scroll driven to completion as the intervening rows are measured, rather than landing on an estimated offset.
- Scrolling and every jump in a long conversation move the viewport again. The list wrapper was dropping the padding that reserves off-screen scroll space, so a 700-turn session's scrollbar spanned about five cards.
- Every reader scroll passes the plain list position rather than an internal virtual row index, so a warm outline, search or keyboard jump scrolls to its target again instead of being ignored as out of range.
- A cold deep link into the middle of a very long transcript no longer hangs the tab at 100% CPU or freezes the reader. One drain runs at a time, its waits yield to the event loop, and each page paints before the next is fetched.
- Message cards are no longer clipped on the right, worst on phones, where the cost footer's trailing text and the cards' right gutter were shaved off. The horizontal reading inset moved to the inner list.
- A tool or Edit card header no longer clips its status badges off-screen at phone width. A long tool name or file basename ellipsizes so the truncated or error status, the server pill and the `+N −M` stat stay visible.
- A very long transcript stays responsive while you reverse-page through it. The reader drops the far off-screen edge of the loaded window past a soft cap and re-fetches it transparently if you scroll back toward it.
- The turn you are reading, your jump target and the current pinned turn are never dropped by that bound, it never applies while a page is still loading, and live-tail stick-to-bottom and the "↓ N new" pill keep working.
- A permalink written in the singular `#/conversation/<id>` form opens the reader exactly like the canonical plural route, instead of landing on the dashboard. Links are still emitted in the plural form.
- A long line of code no longer drags the reading column sideways on mobile. Line-numbered output is caged as its own horizontal scroller with its gutter pinned, and the reading column gets an overflow backstop at every width.
- The `☰ Outline` button works on tablet-width screens. In the 641–1100px band it was dead, because the persistent outline column is hidden there; it now opens the same slide-over sheet mobile uses.
- The session-comparison header shows each run's real title rather than a short id, and the comparison loads both outlines once as a static snapshot instead of re-fetching them on every dashboard refresh.
- The viewer gains a shared design-token layer with all viewer text lifted to an 11px floor, the outline resize divider takes keyboard focus on click, and closing a comparison returns focus to its trigger.
- Live-tail arrivals and the cross-session search error and "Searching…" states are announced to screen readers.

## [1.54.0] - 2026-06-22

### Added
- A `⟷ Compare with…` control puts the conversation list into pick mode and then opens a side-by-side prompt-sequence diff of two runs, for comparing variations of the same task to see where they diverged.
- A metrics-delta strip heads that view with A→B cost, tokens, prompts, errors, duration and files, the two prompt spines are aligned by their first lines with a divergence marker, and clicking a row expands both runs' full prompt text.
- The comparison is two-column on a wide viewport and a single column below about 1100px, conveys divergence by marker and styling rather than colour alone, and is shareable and cold-loadable from its own URL.
- The reader header gains an `Export ▾` menu with four Markdown scopes — whole transcript, prompts only, chat only, and a replay recipe — each offering Copy and Download. The export is computed over the whole assembled session.
- A diff card offers a `.patch` download of a real unified diff, and a Bash card offers a `copy full` action carrying the command, stdout and stderr, with a marker where output was clipped.
- The outline sidebar gains a Files tab listing every file the session modified, in first-touch order, with a summed `+N −M` badge per path, expandable to per-touch rows that scroll the transcript to the change.
- The focus control gains a `▾ More` menu with three further filters on the same axis — Edits, Bash, and a submenu isolating one top-level subagent thread — and `v` keeps cycling only the four primary modes.
- An injected context block carrying an unfenced git diff renders as a real red and green unified diff with a path header and a `+N −M` stat. Detection is conservative, so a Markdown list is never mistaken for a diff.
- When a session's main-thread checklist ends fully completed, the reader header shows a green `✓ Complete · N` chip that jumps to the final checklist, plus an outline landmark. A subagent's own checklist never triggers it.
- Each assistant turn's cost footer gains a micro-bar encoding that turn's cost relative to the most expensive turn loaded so far, and the reader header carries a cumulative-cost chip with a progress bar tracking your scroll.
- The outline's cache-rebuild stat expands from a count into a per-rebuild jump list, each row labelled with its turn, wasted tokens and approximate cost, worst first, honouring the `dashboard.cache_failure_markers` opt-out.
- Each turn's action row gains a ★ bookmark toggle with an inline note editor. Bookmarks persist in your browser's local storage only — nothing leaves the machine — and surface as landmarks in the outline.
- Reader keys `i` and `I` step to the next and previous bookmark, and `t` toggles a bookmark on the current turn.
- A PDF attachment offers a `view inline ▾` toggle that renders it in the reading column at a capped, scrollable height. The bytes fetch only when expanded, behind the same privacy gate as every other transcript media request.

### Changed
- The conversation search Files facet matches a file-path substring rather than a path prefix, so a bare basename such as `package.json` or a mid-path fragment returns the sessions that touched a matching path.
- In regex find mode the in-prose highlight is no longer suppressed. Matches get a best-effort inline underline alongside the existing count and jump-to-match, and a pathological pattern degrades to no underline.

### Fixed
- The reader header's `Export ▾` and focus `▾ More` menus are fully keyboard-navigable: the arrow keys cycle items with wrapping, Home and End jump to the ends, and Escape closes and restores focus to the trigger.
- Git-context diff path parsing stops at whitespace instead of running to end of line, so trailing prose after a path cannot bleed into the displayed header. Real injected diffs, whose paths carry no whitespace, are unaffected.
- A subagent dispatched without an explicit type — the default case — shows its kind, description and token, duration and tool usage in the card chip and the outline. Only explicitly typed subagents did before.
- Jumping to a subagent that was itself spawned by another subagent no longer occasionally leaves that card collapsed. Each card's open state latches in the same render that opens it, so the post-jump reset cannot race it.

## [1.53.0] - 2026-06-21

### Added
- A long conversation opens on its newest turns instead of at the top, with live-tail already following. A short conversation that fits one page still opens at the top so it reads from the start.
- The reader window pages in both directions: scrolling down loads newer turns and scrolling up prepends older ones, anchored so the turn you are reading stays put. A backward page never breaks the follow-the-live-session state.
- Switching away from a conversation and returning restores you to the turn you were last reading, anchored so it survives a resized window or a grown transcript. A deep link still wins, and a vanished turn falls back to the bottom.
- Two reader keys jump to the most recent occurrence: `a` to the last prompt and `L` to the last error. `Latest ↓` and `End` reset to the newest page in a single request rather than paging all the way forward.
- Each subagent thread in the outline shows its cost beside its label, summed once, covering every thread including older ones with no captured spawn metadata.
- A resize divider between the reading column and the outline drags the outline wider or narrower, is keyboard-resizable with the arrow keys, Home and End, and its width persists across reloads.
- When a subagent spawns its own sub-subagents, the outline nests those children indented beneath their parent thread. A session with no such nesting looks exactly as before.
- A conversation compaction is a navigable outline landmark with its own jump chip and the `m` and `M` reader keys. An outline jump chip now goes to the most recent occurrence, and shift-click steps to the previous one.
- A Bash tool card whose output runs past 20 rendered lines opens collapsed with a `show N lines` hint, so it no longer buries the next turn. Short output stays open, and the collapse-all keys still override either way.
- The `f` key focuses the conversation-list search input even while a conversation is open, so you can look up another session without leaving the reader. `/` stays reader-aware and opens the in-conversation find bar.
- The in-conversation find bar opens with `⌘F` or `Ctrl+F` as well as `/`. The browser's native find bar is suppressed only inside the Conversations workspace, and only when no modal or text input is up.
- Two toggles beside the find input, `.*` for regular expressions and `Aa` for case sensitivity, are remembered across reloads and drive a real server-side match over the full transcript. An invalid pattern shows an announced hint.
- The find bar traps keyboard focus while open and its results live-refresh as the conversation grows, keeping your place across the refresh and resetting to the first match only when the one you were on disappeared.
- A Sort control in the conversation rail orders the list by Recent, Oldest, Cost, Messages or Project, and both the sort and the browse filters persist across reloads instead of resetting every session.
- The browse filters apply to full-text search as well, so the Filters button stays enabled with a needle active and the chosen date, project, cost and rebuild axes narrow the search results rather than only the browse list.
- The search chip row gains two structural facets, Title and Files. A Title hit shows the matched session title, and a Files hit leads with the file path and opens the session at that path's most recent touch.
- `cctally doctor` and the dashboard's doctor panel run a conversation-rollup consistency check, warning when the materialized browse rollup disagrees with the conversation cache in a quiescent cache.

### Fixed
- The reader header's `Errors` badge counts error turns, matching the jump chip and exactly what clicking the filter steps between, so it can no longer read higher than the number of places the filter lands.
- A web-search results-count chip no longer shows green when the search errored, the browse list's "Load more" disables while fetching, and the plan-outcome badge no longer reads a reply mentioning "approve" as an approval.
- A subagent whose result exceeds about 16 KB no longer drops its nested subagent into a detached, unlinked card. The link is captured at ingest from the full pre-clip result, and existing transcripts are re-derived once.
- A tool input large enough to exceed the payload cap is actually clipped to that cap rather than served over-cap with only a flag set, and a truncated `stderr` stream now carries its own explicit flag.
- The Recent Sessions table no longer spills past its panel card at narrow two-column widths. It scrolls horizontally inside the card instead, keeping every column full width, with the scrollbar shown only when it cannot fit.
- The Daily heatmap no longer clips the digits of a larger cost. Each cell sizes its cost text to the cell width given the value's length, so a three-digit amount fits whole; wide cells keep the original size.
- `record-credit` refuses a week with an already-applied credit before the confirm prompt rather than after it. You were previously asked to confirm an action that was never going to apply.
- Pressing Escape while focus is on a find-bar button closes only the find bar and restores focus to the transcript, instead of tearing the whole reader down back to the conversation list.
- A Files search hit no longer prints its path twice. The duplicate snippet row is suppressed for file hits, and every other result kind keeps its snippet.

## [1.52.1] - 2026-06-20

### Fixed
- The right-most x-axis tick label on a shared line chart no longer clips at the chart's right edge. Edge ticks are right-aligned while interior ticks stay centred, which matters most for wide labels at narrow render widths.
- `Latest ↓` and the `End` key follow live-streamed turns instead of landing on whichever turn was newest when you opened the conversation. The latest-turn pointer and the last-activity timestamp now track each appended turn.

## [1.52.0] - 2026-06-19

### Added
- On a phone the Daily modal shows prev and next day-stepper controls, because its thirty selectable day bars do not fit. Desktop keeps the full bar row and its keyboard navigation.

### Fixed
- Opening any of the seven dashboard overlays locks page scroll on both the document and the body, so the page no longer scrolls behind the overlay, and each overlay's own scroll region contains its overscroll.
- The Session and Block detail modals reflow their token grid two-up so an eight-digit cache-read value no longer clips, long modal titles ellipsize to one line, and the share composer preview is framed as a labelled document.
- Controls that fell below the 44×44px touch-target minimum at phone width are lifted to it, and the session-row model chip is edge-anchored so it no longer steals taps meant for the row's project link.
- Panels size to their content on a phone instead of reserving the desktop 320px floor, so sparse panels stop showing large dead space, every panel header stays on one line, and the Projects name column gets the flexible track.

## [1.51.0] - 2026-06-19

### Added
- `cctally record-credit --to 31` records an in-place weekly credit the auto-detector misses, when Anthropic lowers your 7-day counter mid-window by a sub-25-point, non-zero amount instead of a clean reset.
- It keeps the same week rather than re-anchoring: it records a clamp floor, lowers the high-water mark, clears stale replays and inserts a post-credit snapshot, so reports and the status line read the credited value.
- It previews and confirms by default, with `--dry-run`, `--yes`, `--from`, `--at`, `--week`, a `--force` clean re-record that never touches real history, and a `--json` envelope. See `docs/commands/record-credit.md`.

### Fixed
- The mobile fix that stops the sticky topbar hiding the reader's `← Back` control now holds on a notched phone. The topbar height budget is measured safe-area-aware, so it tracks the real header height at any inset.
- Because that same measurement is every panel's scroll-anchor floor, an in-page jump lands a panel title flush below the header instead of about 44px under it. Desktop is byte-identical.

## [1.50.0] - 2026-06-18

### Fixed
- On a narrow viewport a conversation transcript no longer shears sideways. The reader's pinned toolbar overflowed the viewport, so any automatic scroll could drift the whole transcript sideways and clip every line off the left edge.
- The session outline no longer buries the transcript the moment you open a conversation on a phone. It defaults closed, opens per conversation, and carries a titled header with a visible close control raised above the sticky topbar.
- A `🔍 Find` toggle sits in the reader controls row at both breakpoints, so the find bar is reachable by touch. Switching conversations clears an open find state, so navigating back no longer reopens the bar and pops the keyboard.
- All seven conversation-viewer text inputs render at 16px on mobile, so focusing one no longer makes iOS Safari zoom the page. Desktop styling is unchanged.
- On a phone the reader header shows de-duplicated, abbreviated model names on one line and clamps the title to two lines, putting more transcript above the fold. Subagent cards get a two-line clamped title.
- A failed conversation-list fetch shows a working Retry button instead of a dead error state.
- About 32 controls across the reader header, find bar, filters popover, rail, outline and transcript body have a 44×44px effective hit area at phone width, with the copy buttons using an invisible centred area so they never cover content.
- A height-calculation bug that let the page scroll about 64px behind the sticky topbar, hiding the reader's `← Back`, is fixed, so that control is reachable at every width, at rest and after a forced scroll.

## [1.49.0] - 2026-06-17

### Added
- The conversation Browse rail gains a `Filters ▾` popover narrowing the list by date, project, cost and number of cache rebuilds. Axes combine with AND, active filters show as removable chips, and filtering is server-side.
- The date filter matches each session's last activity, the instant the rail sorts and groups by, so the sections, the order and the filter agree. A filter set matching nothing shows a distinct message with a Clear filters button.
- A `Latest ↓` action in the reader header, and the `End` key, pages the open conversation forward to its end and lands on the most recent turn, reaching the last turn even in a very long conversation.
- Landing at the bottom parks the reader in its stick-to-bottom position, so a live session keeps following new turns automatically, and the jump waits out a page load that happens to be in flight rather than stopping short.

## [1.48.0] - 2026-06-16

### Fixed
- The conversation viewer renders correctly again after Claude Code moved subagent transcripts to their own files with renamed spawns and null cross-file parent links. All of it is read-time over the existing cache.
- A background subagent card shows completion with token, duration and tool counts, derived from the subagent's own thread and marked with a `~` where Claude Code provided none.
- A nested subagent renders as a recursive nested card, shown exactly once with the spawning tool chip merged in, so a grandchild no longer appears as a detached title-only card beside a stray chip.
- A skill invoked inside a live-watched subagent appears without a manual page refresh, because the live-tail re-fetches a small overlap window and folds the content in.
- A message you typed while the agent was still working now renders. Claude Code queues such a message and stores it as an attachment rather than a normal turn, so the reader silently dropped it.
- Those queued prompts are ingested as ordinary "you" turns in their chronological place, while harness-injected queued items such as background task notifications stay excluded. Existing history is re-derived once.
- Jumping to a deeply nested subagent lands the card aligned to the top rather than centred. A subagent card is often taller than the screen, so centring pushed its head about 250px above the fold.

### Security
- The dashboard's build-time dependencies move up to clear three advisories: `vite` 8.0.10 to 8.0.16 for a `server.fs.deny` bypass and an NTLMv2 hash disclosure in the dev server, and test-only `form-data` 4.0.5 to 4.0.6 for CRLF injection.
- None of those packages reach an installed user, because the npm and Homebrew artifacts carry only the Python CLI and the pre-built dashboard bundle, so there is no runtime exposure. The bundle was rebuilt against the new versions.

## [1.47.0] - 2026-06-16

### Added
- The Recent Sessions modal shows cache-rebuild events for the selected conversation — the rebuild count, wasted dollars, tokens re-created and a session cache-value-saved figure — with worst-first jump links into the conversation viewer.

## [1.46.0] - 2026-06-15

### Added
- The open conversation reader live-tails an active session in near real time, so new turns appear within about a second of the file changing instead of waiting for the periodic dashboard tick.
- Live-tail is on by default and can be turned off with the new `dashboard.live_tail` config key or the dashboard's Settings toggle. `--no-sync` keeps it passive, so frozen-data debugging is unaffected.

## [1.45.0] - 2026-06-15

### Added
- The dashboard marks an assistant turn that suffered a prompt-cache failure, where Claude re-created the bulk of its cached prefix instead of reading it and re-billed those tokens at the higher cache-write rate.
- The failing turn gets an amber `⚡ CACHE REBUILT` chip, and the outline gains a matching landmark, a quick-jump button on the `c` and `C` keys, and a Cache count. Detection is conservative, so a session shows about one marker.
- The markers are on by default and can be turned off with the new `dashboard.cache_failure_markers` config key or the Settings toggle. The wasted-cost figure is a display-only estimate recomputed per read.

## [1.44.3] - 2026-06-15

### Fixed
- `blocks` and the dashboard Blocks panel no longer split a single 5-hour window into two overlapping blocks when Anthropic's reset timestamp jitters by one second across a 10-minute boundary.
- Each window is keyed by the canonical, jitter-collapsed key the recording path already stores, so a straddle collapses to one bucket. The stored rollup was always correct, and the display self-corrects on the next read.

## [1.44.2] - 2026-06-14

### Fixed
- An `Edit`, `Write` or `MultiEdit` card whose input was truncated for transport shows the document's true line count in its header instead of the post-truncation count. A cached conversation picks it up after `cctally cache-sync --rebuild`.

## [1.44.1] - 2026-06-14

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.44.0] - 2026-06-14

### Changed
- The conversation browse rail is served from a small per-session rollup instead of a full-table scan over every message on each page load, so the rail stays fast as your history grows. Its output is byte-identical either way.
- The rail also pauses its per-tick refetch while the tab is hidden and fires exactly one refetch when you come back, so a freshly revealed reader is current without re-querying every five seconds while idle.

## [1.43.3] - 2026-06-14

### Added
- Each conversation is titled by Claude Code's AI-generated session title, in the rail and the reader header, falling back to the first prompt, the project label, then the session id. A title Claude rewrites mid-session updates live.
- A subagent thread is titled by the description of the task that spawned it, in the card header and the outline landmark, and a Bash tool call shows its own description on the chip line, with the command still in the expanded body.

### Fixed
- A short single-line "You" prompt no longer shows an empty gap below its text. The per-message copy and permalink actions float into the bubble's corner instead of padding the bottom of the box.
- The outline no longer highlights two entries at once when you scroll onto a landmark inside a prompt's section. It marks only that landmark rather than also lighting the enclosing prompt.

## [1.43.2] - 2026-06-13

### Added
- `refresh-usage` repaints a locally running dashboard instantly. After a successful force-refresh it asks the dashboard to rebuild, so it reflects the new value within about a second instead of waiting for the next sync interval.

## [1.43.1] - 2026-06-13

### Fixed
- Harness-injected user-role lines — compaction summaries, background task notifications, remote-control stamps and shell-mode echoes — are no longer rendered as your own messages. They show as labelled system pills instead.

## [1.43.0] - 2026-06-13

### Fixed
- A slash command you typed with a real prompt in its arguments is no longer hidden as a system marker. Its arguments render as your "You" bubble with a small command badge, drive the conversation title, and are searchable.
- Bare control commands and empty-argument invocations still fold into hidden system pills. Already-recorded history is corrected at read time, and a migration backfills the search index for past commands.
- Clicking an outline entry selects exactly that entry instead of one a turn or two above it, and the forward jump keys reliably advance to the next landmark instead of re-selecting the same one.
- Clicking a subagent in the outline flashes its collapsed card in place and marks it as the current selection, instead of force-expanding the thread and highlighting the most recent prompt above it.
- The floating "↓ N new" pill no longer counts messages streamed into a collapsed subagent thread you have not expanded. It counts only turns that actually become visible.

## [1.42.0] - 2026-06-13

### Changed
- The conversation outline is redesigned from a flat wall of truncated prose lines into a bold prompt spine with curated landmarks beneath each prompt: section headings, plans, questions, errors and subagent spawns.
- A turn's thinking blocks collapse to a single `🧠 ×N` badge on its prompt instead of doubling the list, session-start plumbing rows are gone, and role glyphs are colour-coded for scanning.
- The outline header is merged into one at-a-glance card, where time, tokens and cost become labelled stat tiles and the formerly cryptic jump chips gain text labels under a "Jump to" row.
- The error count reconciles to one phrase, "14 errors in 13 turns", so the two previously disagreeing numbers no longer read as competing.

### Fixed
- System and slash-command messages were attributed to you as "You" turns, and the first such line became the whole conversation's title. They now fold into collapsed system pills and the title falls back to your real first prompt.
- Terminal escape codes no longer leak into titles, outline labels, or message and command bodies. They are stripped at ingest and again at the render chokepoints, while Bash output keeps its intentional colours.

## [1.41.0] - 2026-06-13

### Added
- Conversation search reaches past prose into commands, file paths, error strings and the assistant's thinking, with an `All · Prompts · Assistant · Tools · Thinking` chip row, a result count, match badges and a `Load N more` button.
- Pressing `/` over an open conversation opens a floating find pill with wrap-around, an exact `k / N` counter and term highlighting. The Tools and Thinking facets light up after a one-time index split runs on the next sync.

## [1.40.0] - 2026-06-12

### Added
- A collapsible outline sidebar, toggled with `o`, lists every turn as a landmark with nested thinking entries and scroll-sync highlighting, above a stats overview of turn counts, duration, tokens, cost, models and tools.
- Jump-to-next keys step through errors, prompts, subagents and plans with no wrap-around, mirrored by a clickable glyph cluster with counts. Focus modes cycle with `v`, and hidden turns coalesce into a clickable marker.
- Each turn header shows a quiet `· HH:mm` in your display timezone, with a `⏸ 42 min later` rule between turns ten or more minutes apart and a date rule when the calendar day changes.
- A per-turn token footer extends the assistant cost line to `$0.0214 · in 1.2k · out 4.8k · cache 310k`, showing tokens only when a turn has usage but no attributable cost.

## [1.39.0] - 2026-06-12

### Added
- An MCP tool call renders an action-first chip — the readable action leads, with a quiet pill for the friendly server name and a per-server icon — instead of the full namespaced name on a generic chip. The original name stays in the tooltip.
- WebFetch and WebSearch render as semantic source cards rather than raw JSON chips. WebFetch shows the domain, a status chip and the Markdown summary; WebSearch shows the quoted query and a list of result titles and domains.
- Images render inline as a lazy-loaded figure with an open-full-size link and a size caption, including screenshots returned inside MCP tool results that were previously dropped. The bytes are served on demand behind the privacy gate.
- A one-time background reingest backfills the new rendering onto your existing transcript history on the first sync after upgrade. It is resumable and lossless, and older rows keep their previous rendering until it completes.

## [1.38.1] - 2026-06-12

### Fixed
- The dashboard no longer pegs one core in steady state on a large cache. The only field readers consume from a per-row JSON blob is materialized into a real column, so the hot read paths parse no JSON.
- A one-time migration backfills that column. Output is byte-identical everywhere it matters, and on a 250,000-row synthetic cache the wide read dropped from about 1.6 s to about 0.35 s per call.

## [1.38.0] - 2026-06-12

### Added
- Edit, MultiEdit and Write render as a unified red and green word-diff card, with a `+N −M` header, intra-line highlighting, per-language colouring on context lines, and a collapsed result panel carrying the real file line numbers.
- Bash renders as a terminal — a `$ <command>` prompt over the output, with stderr split into a red block, a status badge and terminal colours honoured — instead of an undifferentiated blob.
- A truncated tool input or result can be expanded on demand. The affordance re-reads the original session file behind the transcript privacy gate without enlarging the cache, and a deleted source says so clearly.

## [1.37.2] - 2026-06-12

### Fixed
- The web dashboard no longer hangs on startup with a large conversation history. It binds its port and serves the six cost and usage panels immediately, building the first heavy sync on a background thread.
- The one-time conversation-enrichment reingest is resumable. It walks transcript files under a cursor, re-enriching one file per transaction, so an interruption resumes where it left off instead of restarting from scratch.

## [1.37.1] - 2026-06-11

### Fixed
- The conversation viewer's checklist card no longer renders an empty "0 / 0" for tasks created inside a subagent. Those tools record their result as a plain string, and the reader now parses both shapes.
- The running checklist is reconstructed per subagent, so parallel subagents no longer bleed tasks into one another's cards, and a run whose results cannot be parsed falls back to plain tool chips rather than a misleading empty card.

## [1.37.0] - 2026-06-11

### Added
- Claude Code's live to-do tools render as a single checklist card with a progress bar, reconstructed at read time from the create and update stream so the task list evolves turn by turn, instead of separate raw JSON tool chips.

## [1.36.0] - 2026-06-11

### Added
- AskUserQuestion, TodoWrite and ExitPlanMode render as dedicated cards — the question and chosen answer, a live checklist with progress, and the rendered plan with its approve or reject outcome — instead of raw JSON tool chips.

## [1.35.0] - 2026-06-11

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.34.1] - 2026-06-11

### Fixed
- The sticky turn header introduced in 1.34.0 overlapped the prose scrolling beneath it, because its thin opaque mask only half-covered the text. It is replaced by an unobtrusive floating "↑ Top of turn" button.
- That button appears at the bottom right only once a turn's start has scrolled off, clears the "↓ N new" pill, and is hidden on a session switch. Nothing floats over the reading column any more.

## [1.34.0] - 2026-06-11

### Added
- The open conversation updates live. Once you have paged to the end, new turns from an active session appear on each refresh tick, sticking to the newest turn if you are at the bottom or surfacing a "↓ N new" pill if you have scrolled up.

### Changed
- The conversation reader's loading state shows an animated spinner instead of a static glyph, and respects a reduced-motion preference.
- A long turn's header pins to the top while you scroll inside it, and clicking an assistant or human turn header scrolls back to that turn's start.
- The assistant model is shown as a coloured chip matching the rest of the dashboard, instead of plain text. A turn with no known model renders no chip.

## [1.33.0] - 2026-06-11

### Added
- Selecting a conversation or landing a turn jump reflects into the address bar, reload and Back and Forward restore that state, and every turn gains a permalink button that copies a link straight to it.

### Fixed
- The conversation reader nests a skill's content inside its Skill tool call, rather than rendering the loaded body as a separate pill detached from the chip that produced it. A skill injected at session start still gets a pill.

## [1.32.0] - 2026-06-10

### Added
- A subagent thread card shows the subagent's kind and its result meta — tokens, duration, tool count and status — on a modern transcript. An older transcript keeps the title-only card.

### Fixed
- The conversation reader no longer attributes injected content to you. A skill's full markdown body rendered as a large "You" prompt, as if you had typed the entire skill, and so did other harness-injected lines.
- Those now render as quiet collapsed disclosures attributed correctly: a skill body becomes a `Skill content` pill, still full Markdown when expanded, and other injected content becomes a neutral `Injected context` pill.
- Nothing injected is ever shown as a "You" turn, and those bodies no longer pollute conversation titles or full-text search. The fix lands on existing history the next time the dashboard syncs.

## [1.31.1] - 2026-06-09

### Added
- Embedded pricing for `claude-fable-5`. It shipped in the API but was absent from the table, so any session run on it logged an unrecognized-model warning and contributed zero cost to every computation, silently undercounting spend.
- Fable 5 is priced at $10 and $50 per million input and output tokens, with cache rates derived at the standard multipliers and the 1M context window at standard pricing with no long-context premium.

## [1.31.0] - 2026-06-09

### Added
- A tool call's request panel, and a `Read` result's file contents, are syntax-highlighted in the conversation reader. Previously the dominant code surface in a tool-heavy transcript rendered as flat uncoloured text.
- A `Read` result gets a dim line-number gutter in its own column, with the language inferred from the file's extension. Other results stay plain, and an unknown language degrades to plain text.

### Fixed
- The conversation reader uses the full width of its pane. A 68-character measure cap left roughly half of a wide pane empty and wrapped text earlier than the available width, which read as artificial line breaks.

## [1.30.0] - 2026-06-09

### Added
- With a conversation open, `j` and `k` move a focused-turn cursor between turns and auto-load the next page at the end, `[` and `]` collapse and expand every disclosure, and `g` jumps back to the top. They are listed in the help overlay.
- Fenced code in a transcript is syntax-highlighted with a language label, and one-click copy buttons appear on code blocks, tool output and message text.
- Each conversation shows a short derived title in the rail, the reader header and search results, cross-session search matches on that title, and the rail groups conversations under date dividers.
- Jump-to-message expands the owning collapsed subagent thread when the target lands inside one, instead of silently scrolling to a hidden turn.

### Changed
- The conversation viewer is redesigned end to end. Transcripts render as serif prose with higher-contrast text, laid out along a timeline spine with role-differentiated turns.
- An assistant turn is walked in document order, so each tool call renders paired with its result as an inline chip and stray orphan result runs are collapsed. A subagent sidechain renders as a weighted thread card.
- A system-command message folds into an expandable pill, inline SVG icons replace the previous emoji throughout, and disclosure sections open on a smooth animation behind a refined jump flash.

## [1.29.0] - 2026-06-08

### Added
- A full-screen Conversations workspace in `cctally dashboard`: a cost-aware transcript reader with rendered markdown, per-turn cost and collapsible detail, plus cross-session full-text search that jumps to the highlighted message.
- The Conversations workspace is loopback-only by default. LAN access needs `dashboard.expose_transcripts`.

### Changed
- Subagent threads are grouped by their originating agent file, so parallel subagents render as separate collapsible threads with a task label, message count and thread cost, instead of being fused by adjacency.
- The `0700` data-directory hardening covers a stats-first cold start too. The permission was applied when `cache.db` was opened, so a cold start that opened `stats.db` first left the directory at the default umask.

### Fixed
- `cctally dashboard` tears down on a single interrupt signal. About one signal in two thousand raced the wait it was meant to wake, so recovery needed a second one; the wait now uses a self-pipe that the runtime writes on every delivery.
- `cctally db recover --db stats` no longer resets a recovered database's schema version to 0 when a known migration is recorded only under its legacy marker name. Those aliases are normalized before the check.

## [1.28.0] - 2026-06-06

### Added
- `cctally budget` supports per-vendor budgets over configurable calendar periods. The Claude budget can run over a calendar week or month instead of the subscription week, and a separate Codex budget tracks Codex's actual API dollars.
- The two budgets are independent and never summed, because Claude is equivalent dollars and Codex is actual dollars. The status report renders a labelled block per configured vendor with a cost-basis parenthetical.
- `cctally budget set` and `unset` gain `--vendor {claude,codex}` and `--period {subscription-week,calendar-week,calendar-month}`. `--json` gains an always-present `period` key and an additive `codex` object when one is configured.
- A calendar or Codex budget no longer depends on weekly usage snapshots, so a fresh machine with a Codex budget renders `$0` rather than "no usage data yet this week". Codex spend reconciles to the `codex-*` reports.
- A new `codex_budget` desktop-alert axis fires once per threshold as Codex actual spend crosses that percent of the Codex budget, opt-in through `budget.codex.alerts_enabled` and re-arming each calendar period.
- Because Codex usage never flows through `record-usage`, that axis fires from every Claude hook tick and opportunistically whenever you run `cctally budget`, so a pure-Codex user still gets a notification.
- A fired Codex alert appears in the dashboard's Recent alerts and as a toast with a distinct `CODEX` chip and a period-aware label. Preview it with `cctally alerts test --axis codex-budget`.
- Projected-pace budget alerts cover calendar-period Claude budgets and Codex budgets, opt-in through `budget.projected_enabled` and `budget.codex.projected_enabled`, the second of which also requires Codex alerts to be on.
- The dashboard Settings overlay gains the two Codex budget alert switches. Both write through a partial merge, so flipping one never clobbers the Codex amount, period or thresholds, which stay CLI-only.
- The dashboard can optionally serve read-only conversation transcripts through three JSON endpoints, behind a new opt-in `dashboard.expose_transcripts` key that is off by default.
- Transcripts are double-gated: never served unless you have opted in and the request host is loopback-allowed, so a LAN-exposed dashboard never leaks conversation text by default. This release ships the endpoints only.
- `cctally doctor` gains a `db.version_ahead` check warning when a database's schema version has drifted ahead of the running binary, and `cctally db recover` rebuilds an ahead `cache.db` losslessly from source.

### Changed
- `cache.db` and its lock and log sidecars are created with owner-only permissions, and the data directory with `0700`. Conversation transcripts can flow through the cache, so this keeps that data off a shared machine.

### Fixed
- The weekly trend no longer splits a past week into a spurious zero-width row from a single transient `0%` reading. The historical backfill fires only on an unambiguous 25-point drop, while live reset-to-zero detection is unchanged.
- cctally refuses to forward-migrate the production data directory when running from a git checkout, so a development binary cannot brick the installed release. Override with `CCTALLY_ALLOW_PROD_MIGRATION=1`.
- `cctally db recover --db stats` refuses to recover the production stats database when run from a development checkout, leaving it untouched at exit 2, matching the migration guard.

## [1.27.1] - 2026-06-04

### Fixed
- `cctally statusline` with `display.tz = utc` no longer prints a spurious `invalid timezone 'utc'` warning on Linux. The lowercase preference is normalized to the portable `UTC` key before resolution.
- The dashboard's background update-check thread no longer crashes on shutdown. Its internal stop-event field was shadowing a name the thread's own teardown invokes.
- `cctally setup --uninstall` reliably terminates a running legacy usage poller on Linux even when it was launched from a long path, because the process check no longer has its identifying token truncated at 80 columns.

## [1.27.0] - 2026-06-04

### Changed
- cctally supports Python 3.11 and 3.12, not just 3.13, so a distribution whose system Python is 3.11 or 3.12 can run it without a newer interpreter. Homebrew installs are unaffected, because the formula bundles its own Python.
- This was blocked by a one-cent rounding difference between Python versions in a rendered total. Rendered cost and percent totals now route through an exact-summation chokepoint, so every figure is byte-identical on all three.

## [1.26.0] - 2026-06-03

### Added
- `cctally budget set --project` hints the correct argument order when you put the amount after the flag. Writing `budget set --project 25` binds `25` to the flag and leaves the amount unset, and the error now names the supported ordering.
- A bare numeric value that names a real directory, such as a repository literally called `2025`, is treated as a project path rather than a misplaced amount, so the hint never misfires. The exit code is unchanged.

## [1.25.0] - 2026-06-03

### Added
- Per-project weekly budgets. Set a dollar budget for any repository with `cctally budget set 25 --project`, which resolves the current directory's git root, or `--project /abs/path`, and clear it with `cctally budget unset --project`.
- `cctally budget` renders a per-project section below the global status — budget, spent, used percent and verdict, sorted by used percent — even when no global budget is set. It is additive in `--json` and anonymized in share output.
- A new `project_budget` alert axis fires one notification per project and threshold per week, opt-in through `cctally config set budget.project_alerts_enabled true`, with project-specific text and the shared three-tier severity.
- Setting a project budget mid-week when you are already over a threshold records that crossing silently, so only later crossings fire, and a mid-week budget change never re-alerts an already-fired threshold.
- Preview the notification without any real config through `cctally alerts test --axis project-budget --threshold 100`. Projects are keyed by canonical git root, so two repositories sharing a basename stay distinct.
- A fired project alert appears in the dashboard's Recent alerts with a distinct `PROJECT` chip and the project basename, and the Settings overlay gains a per-project budget alerts toggle. Editing the amounts stays CLI-only.

## [1.24.0] - 2026-06-02

### Added
- Threshold alerts dispatch cross-platform, not just on macOS. Alongside the existing Notification Center popup, an alert can fire through Linux `notify-send` with the severity mapped to an urgency, or through a command you configure.
- The backend is picked automatically per host, and `cctally alerts test` prints a `notifier: <resolved>` line so you can see which backend will fire before relying on a real crossing.
- Every spawn passes an argument list with no shell, so alert text containing `$(...)`, `;` or `&&` is passed as one literal argument and can never inject a shell command.
- A new `alerts.command_template` key runs any command on an alert. Set it to a JSON argument list and cctally spawns that command on every crossing, substituting documented tokens for the title, body, severity, urgency, axis and threshold.
- That lets you route alerts to a webhook, a logger, a phone push or any tool you like. The companion `alerts.notifier` key pins the backend explicitly, and both are editable from the dashboard Settings overlay.

### Changed
- Alert severity is a three-tier model. A crossed threshold below 90% is `info`, 90 to 99% is `warn`, and 100% or more is `critical`, and that one mapping drives the toast colour, the panel chip and the Linux urgency token.
- The `alerts.log` audit file gains a seventh column carrying that severity on every dispatch line. Existing alert config and thresholds are unchanged.

## [1.23.0] - 2026-06-02

### Added
- A new opt-in `projected` alert axis warns you before you cross a ceiling, firing on your week-average pace rather than waiting for the actual crossing. It tracks projected weekly percent and projected budget dollars.
- It uses the smooth week-average projection that `forecast` and `budget` already display, and deliberately ignores the hotter trailing-24-hour estimate, so a brief spike does not trigger a false alarm.
- Each level fires once per week, with no re-fire and no recovery alert, is suppressed while the forecast is low-confidence, and re-anchors cleanly across a mid-week reset.
- Both projected toggles default off and are gated behind their parent axis: `alerts.projected_enabled` for weekly percent and `budget.projected_enabled` for budget dollars. Preview either with `cctally alerts test --axis projected`.
- `forecast --json` and `budget --json` expose an additive `week_avg_projection_pct` and `week_avg_projection_usd` field, the exact projection the axis fires on, for scripting and reconciliation.

### Fixed
- The mid-week reset-to-zero detector waits for a corroborating second reading before segmenting the week. It arms a one-tick debounce and fires only if the next reading stays low, treating a bounce-back as a transient API glitch.

## [1.22.4] - 2026-06-01

### Fixed
- A surprise mid-week Anthropic usage reset is reflected in the 7-day percentage even when you were below about 25% usage. The detector only fired on a 25-point drop, so a reset from a lower base slipped through entirely.
- It now also fires on a clean collapse to zero with at least a 3-point drop, independently of the magnitude gate, while that floor rejects the 1%-to-0% replica jitter that would otherwise segment the week spuriously.

## [1.22.3] - 2026-06-01

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.22.2] - 2026-05-30

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.22.1] - 2026-05-30

### Changed
- Exact embedded pricing for 28 more OpenAI Codex `gpt-5.x` models, so their costs are computed precisely instead of approximated. A model absent from the table previously fell back to `gpt-5` pricing and was flagged as an estimate.
- The sync adds every `gpt-5*` variant the upstream snapshot lists but the curated table did not carry, at their exact rates, and backfills the published long-context tier for `gpt-5.5`. No Claude pricing changed.

## [1.22.0] - 2026-05-29

### Added
- `cctally budget` gives you a weekly equivalent-dollar budget with a live pace projection. Set a per-subscription-week target with `cctally budget set 300` and clear it with `cctally budget unset`.
- The report shows spend so far, percent of budget consumed, remaining dollars, current daily pace and a low-to-high end-of-week projection band, with an `ok`, `warn` or `over` verdict and a `LOW CONF` note early in the week.
- Spend is computed live from your session data by the same path `weekly` and `forecast` use, so a pricing edit takes effect immediately. It supports `--json` and the full shareable-output surface. See `docs/commands/budget.md`.
- Actual-spend budget alerts fire a desktop notification when live spend crosses a configured threshold, by default 90% and 100% of the weekly target, recorded once per week and threshold.
- Alerts are forward-only from the moment you set a budget, so a back-dated target reconciles existing crossings as already alerted rather than flooding you with retroactive popups, and a mid-week reset re-anchors the window.
- A `budget` config block — `budget.weekly_usd`, `budget.alerts_enabled` and `budget.alert_thresholds` — is validated separately from the `alerts` block. Alerts are on by default once a budget is set.
- Budget crossings surface in the dashboard's Recent alerts panel, modal and toast alongside the weekly and 5-hour axes, with a distinct `BUDGET` chip, a "$X of $Y budget" line and the actual spend in the Cost column.
- `cctally pricing-check` detects stale or missing embedded model pricing. An unrecognized Claude model silently contributes $0, a silent undercount, and an unrecognized Codex model is only approximated.
- It runs three independently degrading legs: coverage over all your history offline, drift against the upstream price snapshot over the network, and existence against the vendor's model list. `--offline` runs coverage only.
- Exit codes follow a strict precedence: any actionable finding exits 1 even when a network leg degraded, a clean or degraded run with no findings exits 0, and a separate status field reports whether the check was complete.
- `cctally doctor` gains a `pricing.coverage` check that warns when your trailing 30 days contain a model cctally cannot price exactly, listing each model id with its entry count. It is the offline counterpart to `pricing-check`.

### Documentation
- The Codex documentation now states why `cctally codex daily` reports lower token totals than upstream `ccusage-codex` on older sessions, and that cctally is the accurate one.
- Older Codex CLI versions re-emit duplicate token-count records while the cumulative ledger stays flat. Upstream sums every emission, up to about double on the oldest sessions, while cctally counts only events whose cumulative advances.

## [1.21.3] - 2026-05-29

### Fixed
- `cctally doctor` no longer warns about a legacy status-line snippet when your status-line script merely mentions `cctally record-usage` inside a shell comment. A line whose first non-whitespace character is `#` is skipped.
- Homebrew installs no longer create dangling `~/.local/bin` symlinks, and Claude Code hooks point at the version-stable path inside the Homebrew prefix so they survive `brew cleanup`.
- `cctally doctor`'s `install.path` check passes whenever cctally is genuinely reachable on `$PATH` by any channel, and when it does warn the remedy is tailored to how you installed it. Leftover links are reported as stale rather than failed.

## [1.21.2] - 2026-05-28

### Fixed
- An npm upgrade self-heals `~/.local/bin` symlinks for newly added subcommands, so a new `cctally-*` binary is reachable immediately after the install without re-running `cctally setup`.

### Changed
- `cctally doctor` and `cctally setup --status` report subcommand symlinks as available and treat a command reachable on `$PATH` through another install channel as present, instead of warning only because `~/.local/bin` lacks the link.

## [1.21.1] - 2026-05-28

### Fixed
- `cctally statusline` shows the correct 7-day percentage after a mid-week reset instead of staying pinned to the pre-reset peak. The status line read `7d 41%` while `report` and the dashboard correctly showed 2%.
- The high-water clamp keyed only on the week's start date, and post-reset snapshots keep the same start date, so the maximum returned the stale peak. The clamp now floors to snapshots captured at or after the latest reset.

## [1.21.0] - 2026-05-28

### Added
- Claude Opus 4.8 pricing, at the standard Opus 4.x rates, so every cost-computing command prices those sessions correctly instead of warning about an unknown model and treating the cost as $0.
- The 1M-context variant is added to the context-window table, so the status line's context percentage measures against the real 1,000,000-token window rather than falling through to the 200K family default.

## [1.20.4] - 2026-05-28

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.20.3] - 2026-05-28

### Fixed
- The dashboard's Blocks panel no longer renders a just-started active block with a `~` heuristic marker, and the previous block no longer silently disappears from the panel about 20 minutes after a 5-hour reset.
- Both came from the same cause: two genuinely adjacent canonical windows whose floored keys landed less than five hours apart were treated as conflicting, so the scheduler dropped one. A canonical window is now always restored.

## [1.20.2] - 2026-05-28

### Fixed
- `cctally record-usage` rejects an implausibly dated `--resets-at` epoch before it can poison the stored history. A real 366-day-off payload silently wrote a phantom-year row that displaced later weeks in `cctally report`.
- An already-expired 5-hour window is no longer charged against the previous block: those fields are dropped and the weekly snapshot still writes. A guard miss is surfaced rather than silently reported as success.

## [1.20.1] - 2026-05-28

### Fixed
- `cctally statusline` computes its `today` segment over your local calendar day, matching every other reporting command. It hardcoded UTC, so for anyone at an offset it dropped entries in the band after local midnight.

## [1.20.0] - 2026-05-28

### Added
- `cctally statusline` emits a one-line status string for Claude Code's `statusLine` hook, as a drop-in for `ccusage statusline` with cctally extensions appended.
- It emits five segments: the model name, session, today and block cost, the burn rate with an optional colour cue, context-window utilization, and a cctally `5h X% · 7d Y%` extension sourced from the hook's own rate limits.
- The flag surface mirrors upstream: `-B` for the burn-rate visual, `--cost-source`, the two context thresholds, `-z/--timezone` and `-d/--debug`, plus documented no-op aliases and a real `--config PATH` override.
- cctally adds `--cctally-extensions` and `--no-cctally-extensions`, on by default, and persists three `statusline.*` config keys, with the command line taking precedence over config and config over the built-in default.
- Context percent is computed from a memory-safe tail-walk of the transcript, dividing the last assistant turn's usage by the model's context window. An unknown model renders `🧠 N/A` with a one-shot warning.
- The stdin contract is deliberately graceful: only malformed JSON or a non-object root exits 1. Every other missing field produces a degraded but working line at exit 0, so the status line never fails the hook on a partial payload.

## [1.19.0] - 2026-05-28

### Added
- `cctally blocks` gains the drop-in flags `-a/--active`, `-r/--recent`, `-t/--token-limit` and `-n/--session-length`. `-a` filters to the single live block and renders a detail box; with no active block it prints a message and exits 0.
- `-r` keeps only blocks from the last three days plus the active one. `-t N` keys the table's percent, remaining and projected columns to an explicit limit, while `-t max` derives it from the largest completed block.
- `-n` is accepted for compatibility but does nothing, because cctally's blocks follow Anthropic's real five-hour resets and are not re-sizable. A value of zero or less is an error.

## [1.18.0] - 2026-05-27

### Added
- `cctally daily` gains the project-axis flags `-i/--instances`, `-p/--project` and `--project-aliases`, as a drop-in for `ccusage daily`. `-i` groups the report by git root with one global total.
- `-p PATTERN` filters to matching projects by case-insensitive substring and is repeatable with OR semantics, and `--project-aliases` overrides display labels in the table headers only, never the JSON keys.
- Two git roots that share a basename stay separate, rendering as `app (work)` and `app (personal)`, and entries with no project collect under `(unknown)`.

## [1.17.0] - 2026-05-27

### Added
- `--speed {auto,standard,fast}` on the Codex reports. `auto`, the default, reads your Codex service tier from its own config and applies fast-tier pricing when it is `fast` or `priority`, and `fast` or `standard` force the tier.
- Fast-tier multiplies the per-model Codex cost by a fixed factor of at least 2×, and 2.5× for `gpt-5.5`. `--json` gains no new field; only the cost figures change.

### Changed
- The Codex reports apply fast-tier pricing by default when your Codex config sets a fast or priority service tier. cctally always priced at the standard tier before, under-reporting cost for anyone paying that premium.
- Those costs now match what was actually billed. Pass `--speed standard` to force the old behaviour, and anyone without a fast or priority tier sees identical numbers.

## [1.16.0] - 2026-05-26

### Added
- `-m/--mode {auto,calculate,display}` selects the cost source on `cctally daily`, `monthly`, `weekly`, `session` and `blocks`, as a drop-in for `ccusage <cmd> --mode`.
- `auto`, the default, uses the recorded cost from the session file when present and otherwise computes from embedded pricing. `calculate` always computes, and `display` shows only the recorded cost and renders `$0.00` when there is none.
- Because most modern Claude Code session files omit a recorded cost, `display` reports `$0` for nearly everything. `cctally five-hour-blocks` accepts `--mode` as a documented no-op, since its cost is materialized at record time.

### Changed
- `cctally session` prefers a session's recorded cost over a recomputed one by default, for the historical sessions whose files still carry it. It was the one report that always recomputed, so it disagreed with `cctally daily`.
- Only the roughly 4% of historical files that still carry a recorded cost are affected. Pass `cctally session --mode calculate` to force the previous always-recompute behaviour.

## [1.15.0] - 2026-05-26

### Added
- `cctally claude <cmd>` and `cctally codex <cmd>` let you paste ccusage's hierarchical syntax verbatim. Each leaf routes to the same engine as its flat form, so the table, `--json` and exit code are byte-identical and only `--help` differs.
- The flat forms remain fully supported as back-compatible aliases, with no deprecation warning. `codex weekly` is a cctally extension, because upstream has none.

## [1.14.0] - 2026-05-26

### Added
- Running `cctally` from a git checkout uses a separate `~/.local/share/cctally-dev/` data directory, so developing against the source tree can no longer corrupt the installed instance, and vice versa.
- A checkout is auto-detected and transparently relocated. The npm and Homebrew copies ship without a git directory, so installed users are byte-for-byte unaffected, and your session files, settings and credentials stay shared read-only.
- `cctally doctor` reports whether it is the installed copy or a checkout plus the resolved data directory, `cctally --version` shows a marker, and `cctally setup` refuses to wire a checkout into your settings unless given `--force-dev`.

### Changed
- `cctally session`'s `totalTokens` sums all four token components — input, output, cache create and cache read — matching `daily`, `monthly` and upstream. It counted input and output only, leaving the roll-up about 99% below upstream.
- The `--json` field name and shape are unchanged; only the value widened to include cache. `codex-session` deliberately keeps input plus output, because Codex already counts cache inside its input figure.

### Fixed
- `cctally codex-session` no longer misaligns its table on a narrow terminal, including the default 120 columns. Numeric columns keep their full value, header and text labels ellipsize to fit, and every box line shares one width.
- The stats database's write paths are hardened against a "database is locked" crash under concurrent multi-process use. The one-time 5-hour backfill and the live block update now take the write lock before their first read.
- The cache-rebuild migration can no longer corrupt session history when it runs at the same time as a cache ingest. It takes the same lock the ingest holds, so the wipe and the ingest walk are mutually exclusive.
- `cctally refresh-usage` no longer crashes when the current 5-hour window is inactive. A window reported with no reset timestamp is dropped instead of being fed into the window-key derivation.

## [1.13.0] - 2026-05-25

### Added
- Every Claude reporting command accepts the ccusage flag surface, so an `ccusage <cmd> [flags]` invocation pastes into `cctally` unchanged. The pass is purely additive and no existing output changes.
- Across all ten reporting subcommands, `-z/--timezone` aliases `--tz`, the date-taking commands accept `--since` and `--until` in both `YYYY-MM-DD` and `YYYYMMDD` forms, and `--compact` forces the compact table layout.
- `--color` and `--no-color`, plus the `NO_COLOR` and `FORCE_COLOR` environment variables, control ANSI output on the colour-emitting commands and are accepted but inert elsewhere. A top-level `-v` alias for `--version` is added.
- `cctally session` gains `-i/--id <session-id>` to filter to a single session. The match is exact against the post-resume-merge id, and an unknown id renders empty and exits 0.
- `--config <path>` is a real per-invocation override. It loads configuration from the given path for that invocation only, leaving your persisted configuration untouched.
- `-d/--debug` emits a real pricing-mismatch report on stderr for the Claude reporting commands, comparing each entry's recorded cost against the token-recomputed cost. `--debug-samples N` caps the sample block.
- The report goes to stderr only, so `--json` and `--format` pipelines stay byte-stable, and `diff` emits one report per window.
- The Codex reports accept `-d/--debug` and `--debug-samples N` too. Because Codex records no cost to compare against, the report gives totals plus the highest computed-cost entries, tagging fallback-priced models.
- `--compact` reshapes output on the five reporting commands where it was accepted but inert: `five-hour-blocks`, `project`, `diff`, `range-cost` and `cache-report`.

### Fixed
- `project --compact` and `session --compact` no longer corrupt numeric values or overflow the terminal at narrow widths. A token count like `12,345,678` rendered as the silently wrong `12,345,…`.
- Numeric columns are now floored at their full value width and never truncated, because a wrong number is worse than honest overflow, while text columns and their header labels absorb the squeeze.
- `cache-report --since` and `--until` accept space-separated datetimes and ISO week-dates again. A refactor had narrowed the parser to two date forms and silently rejected everything else it used to take.
- `diff` and `project` no longer emit ANSI colour into a piped or redirected stdout when `CI` is set. The `CI` rung sat above the terminal check, so a redirect on a CI runner captured raw escape sequences.

## [1.12.0] - 2026-05-24

### Fixed
- Deduplicating streaming and post-stream session rows now picks the post-stream finalization, matching upstream. Earlier versions kept the first emission, which on a tool-using turn is an intermediate reporting one output token.
- That caused a systematic undercount of output tokens on agentic workloads of roughly 60%, worth about $5 of missing cost per active block on Opus 4.7. cctally now picks the row with the higher token total.
- An upgrade from before this release actually runs the new dedup migration instead of being marker-only. The fresh-install shortcut classified every older cache as fresh and stamped the migration without running it.
- `cctally db skip 001_dedup_highest_wins` no longer traps the dependent stats recompute in indefinite deferral. An explicit skip is now treated the same as an applied marker by the gate that consults it.
- A cache row stays attributed to whichever session file first inserted it, even when a later file wins the dedup contest. The attribution used to flip, which silently moved usage between project buckets on every swap.
- Truncating a session file no longer drops dedup-winning rows from the cache. A truncation now forces every file to re-ingest from the start, which is safe because the cache is fully re-derivable.
- The dedup recompute refuses to run against a cache that is empty, partially rebuilt, or still holding rows for a deleted session file, so it can never silently zero your historical weekly cost, block totals or milestones.
- Interrupting a sync no longer lets that recompute certify an incomplete cache. A completion marker is written only after a full clean walk, and it is cleared by a rebuild, a truncation or a tracked file disappearing from disk.
- The re-ingest banner is suppressed on machine-consumed surfaces such as the status line and the hook, and on an empty cache where the migration has nothing to announce. An interactive command still sees it once.
- The ingest progress line reads `N rows changed` rather than `N new rows`, because under the new dedup the count covers both new rows and replacements.
- A documented limitation: the one-time recompute reflects only the session files surviving on disk at the moment of upgrade. A date range whose files have been pruned recomputes to `$0`, and a partially pruned range undercounts.

### Changed
- `report` historical costs are lower, and correct, after the upgrade. Every stored weekly cost snapshot is recomputed from the corrected entries, so `report` and `weekly` now agree on historical figures.
- `five-hour-blocks` historical totals are lower, and correct, after the upgrade. The live writer only recomputed the active block, so closed historical blocks would otherwise carry the inflated totals forever.
- `percent-breakdown` milestones from before the fix show lower, corrected cumulative cost after the upgrade. This is a one-time exception to the write-once rule; forward-going behaviour is unchanged.
- `five-hour-breakdown` rows from before the fix stay as recorded, and a recompute is deferred to a follow-up. New 5-hour milestones from the upgrade forward are correct.

### Migration notes
- The first command after upgrading wipes and re-ingests the session cache. Expect a 10 to 30 second pause on that first command for a typical history.
- The three stats recomputes are gated on that cache migration. On a machine with session files on disk they defer one more invocation while the next reading command repopulates the cache; with none, they complete immediately.
- Run `cctally db status` to verify all three migrations applied.
- A weekly cost snapshot with no recorded range is skipped by the recompute, and its pre-fix value persists. Delete the row and re-run `sync-week` if you need it recomputed.
- A percent milestone with no recorded week start is skipped too, on legacy schemas only, and its pre-fix value persists.
- To defer any of them, run `cctally db skip <name>` with a `--reason`, and reverse it with `cctally db unskip`. Your numbers stay pre-fix until you do.

## [1.11.1] - 2026-05-22

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.11.0] - 2026-05-22

### Added
- A new dashboard Cache Report panel and modal expose the `cctally cache-report` anomaly and savings surface as an always-on watchdog. The panel turns amber when today crosses an anomaly trigger.
- The panel carries a 14-day cache-hit-rate sparkline and a 7-day "+$X saved · N ⚠ days" subline, and clicking it opens a modal with today's spotlight, the full timeline, per-day net dollar bars and a counterfactual callout.
- The modal also carries the daily rows table with per-column colouring and by-project and by-model breakdown sub-cards. On a fresh install the panel sits at the end of the board; an existing saved order gets it appended.
- The dashboard settings endpoint accepts a `cache_report` block with an `anomaly_threshold_pp` key, which the modal's settings popover round-trips and which is re-evaluated on the next sync. An invalid value returns a 400.

### Fixed
- `cctally cache-report` buckets its daily rows by the resolved `display.tz` instead of the host's zone. A non-host timezone shifted the window edges without shifting the dates rendered in the table.
- A panel at position 11 or later in the help overlay renders an em dash for its shortcut rather than a literal `11` key chip, because the digit shortcuts only reach the first ten positions.
- The Cache Report panel, modal and spotlight all stay neutral during the baseline-building window of the first four captured days. The classifier can fire without a baseline, so the surface contradicted itself.
- The by-project and by-model breakdown cards aggregate only over the same dates as the displayed 14-day table. A rolling window can emit 15 buckets in a non-UTC zone, and the cards silently included the dropped one.
- The daily-rows Cache % cell colour tracks the same 5-point band the sparkline draws around today's median, so a day visibly outside the highlighted band is no longer painted green.
- The Flag column stays bound to each row's own anomaly verdict, so the display band and the classifier are independent signals that each carry their own meaning.
- The anomaly-threshold popover rejects fractional input inline rather than silently truncating it and sending a different value than you typed.

## [1.10.3] - 2026-05-21

### Fixed
- A `blocks` gap row renders the correct duration. Every gap shorter than 30 minutes surfaced as `1h gap`, so a five-minute gap read the same as a fifty-minute one. A sub-hour gap now reads `Nm gap`.
- A gap shorter than 60 seconds is suppressed entirely, so a one-second seam between two adjacent windows no longer emits a row.

## [1.10.2] - 2026-05-21

### Fixed
- The `blocks` panel and CLI no longer render a phantom active block when a canonical 5-hour reset does not fall on a 10-minute boundary. Entries in the band before the reset fell into neither bucket and became a second active block.
- Both the entry partition and the recorded block now use the canonical window from storage, jitter intact, so clicking a block whose reset is off the boundary opens its exact window instead of returning a 404.

## [1.10.1] - 2026-05-20

### Fixed
- The committed dashboard bundle matches its source again. Three Projects-modal fixes were applied to the source but the rebuilt bundle was never committed, so none of them reached a browser.

## [1.10.0] - 2026-05-20

### Added
- A new dashboard Projects panel and modal: a top-five current-week leaderboard with horizontal bars and an attributed `Used %`, and a modal with a `1w` / `4w` / `8w` / `12w` selector, a stacked-area trend chart and a seven-column table.
- The modal drills in place into a project's model breakdown and recent sessions, and navigates both ways with Sessions. The `5` shortcut opens it directly, and three new share templates carry the active window into the share flow.

### Fixed
- The Projects modal is no longer cramped on a phone. The seven-column table reflows to stacked cards, a sort-cycle pill replaces the column header, and the per-project drill renders inline under the selected card.
- The Projects modal's `$/1%` column is replaced with `% of week`. The old column was mathematically degenerate: the same value appeared in every row at every window, adding no per-row signal.
- The Projects trend chart renders visibly when only one week is in scope. Every point collapsed to the middle, drawing each series as a zero-width line, so the chart looked empty while the table showed real activity.
- The Projects panel populates correctly after a mid-week reset. The bucket lookup used the shifted week start while the aggregator anchored rows to the ISO Monday, so it missed every bucket and reported no rows.
- The Projects panel no longer overstates `Used %` on a credited week. It read the maximum percentage for the week, which returns the stale pre-credit peak; it now reads the latest snapshot instead.
- The window selected by the Projects modal's pill reaches the server, so a share taken at `4w` is no longer rendered as `1w` regardless of what you chose.
- The Projects modal's Sessions, First seen and Last seen columns are window-scoped, reflecting the active pill instead of fixed twelve-week and all-time values.
- The Projects trend chart's legend on a phone is a horizontal scrolling row with `(other)` pinned last, a right-edge fade and tap-to-drill on each item, replacing a silent two-line clamp.
- A thin-history dashboard no longer mis-labels an exported artifact. A fresh install requesting the twelve-week template rendered as "Last 12 weeks" over a three-week date range; the label now matches the rows it carries.
- A descending sort on a nullable Projects column keeps its `—` rows pinned at the bottom. The null-parking constants were being flipped along with the direction, which pulled those rows to the top.
- The Projects modal's keyboard shortcuts no longer leak through the share or composer overlay layered above it, which silently changed the window or the selected row on the modal underneath.
- The project drill endpoint scopes its attributed percentage to the requested window. It summed the full twelve-week array, so a `1w` or `4w` drill reported the twelve-week total.
- The Projects drill treats a window change on the same project as stale, so switching the pill no longer leaves the previous window's cost, models and sessions rendered under the new heading while the request resolves.
- The Projects modal re-binds a persisted selection to the leader, or clears it, when the previously selected project drops out of the current window's rows, instead of pointing the drill at a row that no longer exists.
- A mid-week Projects share no longer advertises a period ending at a future reset the rows do not include. The end is clipped to now, matching the live dashboard's spend figure.
- The Projects modal's mobile sort cycle defaults `first seen` to ascending, matching the desktop column header, so the two no longer produce opposite orders and a persisted desktop sort is representable on mobile.

## [1.9.0] - 2026-05-19

### Removed
- The `cctally release` subcommand. Release automation is maintainer-only now, so an npm or Homebrew install no longer carries it. A stale symlink from an earlier version is cleaned up by `cctally setup --install`.

### Fixed
- `cctally setup` and `cctally setup --uninstall` retire a legacy `cctally-release` symlink left by an older install, including one whose target points into a Homebrew keg or an npm tree that lingers on disk until a cleanup.
- A hand-made link to your own checkout is still preserved, because its target is neither dangling nor under a foreign install root.

## [1.8.2] - 2026-05-18

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.8.1] - 2026-05-18

### Fixed
- A row annotated with the `⚡` credit marker in `cctally five-hour-blocks` and `five-hour-breakdown` no longer pushes the table's right border one cell out of line. The marker is one code point but renders two terminal cells.

## [1.8.0] - 2026-05-18

### Changed
- The npm package description and the README header front-load the terms people actually search for, while keeping the cost-per-percent hook, and five package keywords are swapped for higher-intent ones.

### Fixed
- The dashboard's weekly panel footer total matches the rows above it on a credited week. The rows were split at the credit while the footer read the snapshotted total over the original whole week, so the two disagreed.

## [1.7.4] - 2026-05-17

### Fixed
- `cctally blocks` no longer re-introduces a phantom `~` row when two in-place credits land inside the same 5-hour window. The credits are sorted before the pick, so the earliest one is chosen consistently.
- The cleanup that scrubs stale replayed snapshots out of a post-credit segment tolerates rounding drift, on both the weekly and the 5-hour axis, instead of requiring an exact match that a differently rounded payload would slip past.

## [1.7.3] - 2026-05-16

### Added
- `cctally record-usage` detects an in-place 5-hour credit: a drop of at least 5 points inside the same window while its reset is still ahead. It records the credit, lowers the high-water mark and scrubs stale replays.
- Stacked credits are supported across distinct 10-minute slots within one 5-hour block. Two credits in the same slot collapse into one, which is a documented bound.

### Changed
- The 5-hour clamp is credit-aware, so a fresh post-credit reading of 4% lands instead of being held back by pre-credit history of 28%. A database with no credit events behaves exactly as before.
- `cctally five-hour-blocks` shows an inline `⚡ credited −Xpp @ HH:MM` chip beside the block start on a credited row, and several credits in one block concatenate. The chip carries through `--json` and all three share formats.
- `cctally five-hour-breakdown` interleaves a `⚡ CREDIT −Xpp @ HH:MM` divider between the pre-credit and post-credit milestones, and each milestone in `--json` carries the segment it belongs to.
- The dashboard's 5-hour panel and Current Week modal show the same credit chip and milestone section, and a post-credit crossing of a threshold appears as its own alert row rather than being folded into the pre-credit one.
- A credit is recorded reliably even when a previous run committed the event and then died. The follow-up steps are idempotent and run on every detection, rather than being skipped and leaving the pre-credit peak in place.
- Detecting a second credit after an idle gap works. The duplicate check compares both the before and after percentages of the most recent stored credit, where comparing one of them alone silently swallowed the second credit.
- The dashboard's weekly alert context exposes the credit segment alongside the 5-hour one, so a consumer can tell a pre-credit crossing from a post-credit crossing of the same week and threshold without parsing the alert id.

## [1.7.2] - 2026-05-16

### Fixed
- `cctally record-usage` detects an Anthropic-issued in-place weekly credit, where your utilization drops while the reset stays put, so the dashboard, forecast, report, percent-breakdown and terminal UI stop freezing at the pre-credit peak.
- Detection fires on a drop of at least 25 points, the same threshold as the existing path that catches a mid-week boundary shift, and a credit triggers exactly once.
- The monotonic 7-day clamp is credit-aware, so a fresh reading lands instead of being held back by pre-credit history. A week with no credit event behaves exactly as before.
- The historical backfill detects past in-place credits in an existing database using the same rule as the live path, so your earlier credited weeks are repaired without affecting anything else.
- A stale pre-credit percentage replayed by an external status-line tool is scrubbed from the post-credit segment, so it can no longer block a legitimate fresh value from landing.
- `cctally percent-breakdown`, the dashboard milestone panel and the terminal UI filter milestones by the active credit segment, so a credited week's header and its body finally agree.
- An empty post-credit segment renders a distinct "no milestones crossed yet" hint, so you can tell a freshly credited week from a genuinely silent one.
- A post-credit crossing of 1%, 2% or 3% fires its alert fresh even when the pre-credit ledger already crossed those thresholds, because each crossing is now recorded against its own segment.
- `cctally doctor` gains a check that warns when a credited week has usage but no post-credit milestones yet, which surfaces the gap between the credit landing and your next recorded crossing.
- `cctally report` and `weekly` render a credited week as two trend rows, one per segment. Only the post-credit segment surfaced before, so the bulk of that week's spend was silently dropped from the table.
- `cctally report`'s current-week summary box shows the post-credit row rather than the closed pre-credit one. On the reporting user's database it showed 67% where the truth was 4%.
- The dashboard's trend and Weekly panels render a credited week as two rows too, each with its own percentage and cost, instead of collapsing both segments onto the post-credit value or dropping the pre-credit interval.
- `cctally blocks` and `cctally five-hour-blocks` agree on the API anchor for the active 5-hour block, so `blocks` no longer shows a heuristic `~` prefix while the other view shows the real reset.
- That swap re-aggregates the block's tokens and cost over the canonical interval too. The displayed window and its totals came from different intervals, showing $45.42 where the real cost was over $128.
- `cctally blocks` no longer renders a phantom heuristic block between two real blocks after a credit. A credit creates two overlapping windows, and the earlier one is now truncated at the credit moment rather than dropped.

## [1.7.1] - 2026-05-15

### Fixed
- The weekly bucket key is anchored on the canonical UTC calendar day rather than the host's local one, so a process that briefly inherits a non-UTC timezone can no longer fork one subscription week into two rows in the trend table.
- A self-heal migration merges any already-forked rows on the next open, and a new `data.forked_buckets` doctor check reports the invariant with per-table counts so the next regression is visible immediately.
- On a Homebrew install, `cctally --help`, `cctally doctor`, the share GUI and the `--format` flag no longer crash looking for runtime modules. The formula installed a fixed list of names, so several modules never reached the install.
- Those surfaces have been latently broken on every Homebrew install since v1.4.0, and a packaging guard now stops a future module from silently dropping out of that layout.
- The version self-heal no longer corrupts the global update state when cctally is run from a development clone, which used to stamp the clone's version over the installed one until you deleted the state file.

## [1.7.0] - 2026-05-13

### Added
- `cctally doctor` is a read-only diagnostic subcommand consolidating install, hooks, OAuth, database, freshness and safety state into one severity-ranked report, in human and JSON form. It exits 0 unless a check fails, then 2.
- The dashboard exposes the same diagnostic through an aggregate-health header chip and a full-report modal, opened by clicking the chip or pressing `d`.

### Changed
- The detail share templates for `weekly`, `daily`, `monthly` and `blocks` ship cross-tab data in their Markdown and HTML exports. SVG output for those templates still omits the table body.

### Fixed
- The dashboard version label and CLI banner no longer stay frozen on the pre-upgrade version after an out-of-band install. A self-heal compares the running binary's changelog against the recorded state and re-stamps it when they disagree.
- `cctally update` no longer stamps the wrong version. It stamped a cached probe result rather than what was actually installed, and now prefers the freshly installed changelog.
- On a Homebrew install, `cctally update` with no explicit version no longer stamps the pre-upgrade version, because the running process still reads the old install's changelog.
- `cctally doctor` no longer warns that a deferral file has bad types when it holds exactly the shape `cctally update --remind-later` writes, which had it recommending you delete a perfectly valid file.
- The dashboard's global keys no longer fire underneath an open Doctor modal, which used to pop panel modals into place or quit the dashboard behind a still-visible card.
- `cctally doctor`'s symlink check no longer reports every entry missing when the running invocation belongs to a different install than the one that owns the links. It now asks whether each command is invokable from your path.

## [1.6.3] - 2026-05-12

### Fixed
- The v1.6.2 npm tarball was built from a pre-fix tree and lacked one module, so v1.6.3 republishes the same content under a fresh version. Homebrew users on v1.6.2 are unaffected.

## [1.6.2] - 2026-05-12

### Fixed
- The dashboard share GUI works on an npm install. One module needed to reach the published package as well as being listed in it, and a packaging guard now checks both layers so a future addition cannot ship half-configured.

## [1.6.1] - 2026-05-12

### Fixed
- An npm-installed cctally ships the two share modules the dashboard loads at run time. They had been absent from the package since v1.4.0, so the share GUI failed with "Couldn't load templates" on every panel.

## [1.6.0] - 2026-05-12

### Added
- A dashboard share GUI. A per-panel `↗` icon opens a modal with 24 infographic templates, a live preview, themed export to Markdown, HTML and SVG, client-side PNG, and the browser's own Print to PDF.
- `S` shares the focused panel and `B` opens the basket composer.
- The composer collects template recipes from any panel into a basket, capped at 20 and remembered in your browser, then stitches them into one document with a single title, one frontmatter block and one footer.
- A composed section shows "Outdated" when the data behind it has moved, and refreshing one section re-renders it without losing the basket's order.
- Share presets and history: save the current template and its settings under a panel-scoped name, and recall a preset or one of your last 20 export recipes from the gallery's dropdown.

### Changed
- A Markdown export carries YAML frontmatter with its title, generation time, period, panel, privacy mode and cctally version. `--no-branding` strips it.

### Docs
- A new reference page documents the share GUI.

## [1.5.0] - 2026-05-11

### Added
- `cctally update` self-updates an npm or Homebrew install, with a suggestion banner in the CLI and an amber badge in the dashboard, plus `--check`, `--skip`, `--remind-later`, `--version`, `--json` and `--dry-run`.
- A source or development install falls through to a manual recipe instead. The dashboard's update modal streams the live output and survives the restart.
- `update.check.enabled` and `update.check.ttl_hours` let you opt out of automatic version checks or extend the 24-hour default up to 30 days.
- `cctally setup` detects hooks left by an earlier install pattern and offers to migrate them: it unwires the matching entries, moves the files to a timestamped backup directory, and stops any background daemon they had spawned.
- The move is reversible, because files are moved and not deleted, and the backup directory is created before the settings write, so a failure leaves your settings byte-identical rather than half applied.
- Before stopping a daemon, the migration verifies the recorded process really is the legacy one, so a stale marker pointing at a recycled process id cannot kill something unrelated.
- `--migrate-legacy-hooks` and `--no-migrate-legacy-hooks` control it non-interactively, `setup --status` reports the migration state, and `--dry-run` previews it without touching disk.
- `npm install -g cctally` prints a one-time hint pointing at `cctally setup`, mirroring what Homebrew already shows. It never runs setup for you, and a per-project install stays silent.

## [1.4.0] - 2026-05-09

### Added
- Shareable reports. All eight reporting subcommands accept `--format md|html|svg` and emit an artifact to a dated filename, with `--theme`, `--no-branding`, `--reveal-projects`, `--output`, `--copy` and `--open`.
- Project labels are anonymized to `project-N` unless you pass `--reveal-projects`, and `session --format` also takes `--top-n N` to cap the chart's project breakdown. See `docs/commands/share.md`.

### Fixed
- The dashboard's 5-hour row shows the post-reset delta when a block spans a weekly reset, instead of hiding the number behind a reset line. It previously showed a misleading negative delta on any host outside UTC.
- `record-usage` self-heals milestone and 5-hour block rows that were dropped when an earlier run was killed between two inserts, recovering them on the next status-line tick rather than waiting for your percentage to advance.

## [1.3.0] - 2026-05-08

### Maintenance
- Internal improvements only; no user-facing changes.

## [1.2.0] - 2026-05-08

### Added
- An npm distribution channel. `npm install -g cctally` lands the Python script and the dashboard assets through a thin Node shim.
- A Homebrew distribution channel: `brew install omrikais/cctally/cctally`, through a separate tap. The formula depends on its own Python and pins cctally's interpreter to it.

### Fixed
- `cctally setup` writes hook commands through the same channel-aware resolver it uses for the main symlink, so an npm install gets the Node shim path. Without it, every hook silently failed for anyone whose system Python was too old.
- `cctally setup` no longer points the main symlink at the Node shim during a source-clone install, which left a broken `cctally` on the path for anyone without Node.

## [1.1.0] - 2026-05-07

### Added
- `cctally setup` is a one-command install: it symlinks the user-facing binaries into `~/.local/bin/` and adds additive hook entries to your Claude Code settings, with `--dry-run`, `--status`, `--uninstall` and `--uninstall --purge`.
- `cctally hook-tick` is the internal per-fire runtime the hooks invoke. It reads the hook payload, syncs the cache and refreshes usage under a throttle.
- Hook activity is written to a rotating log under your data directory, capped at 1 MB with a single generation kept.
- `cctally db status` lists applied, pending, failed and skipped migrations per database, with `--json`.
- `cctally db skip <name> [--reason …]` bypasses a migration that genuinely cannot succeed on a particular machine, and `cctally db unskip <name>` removes the mark so it retries on the next open.
- A migration failure is recorded and surfaces as a banner on your next interactive command, and the banner clears automatically once that migration succeeds.

### Changed
- Hooks are the default integration now. The legacy status-line snippet is no longer the recommended path but remains fully supported as an opt-in alternative.
- The three pre-framework migrations are managed by the framework, so an existing database renames their markers on the next open and `db status` and `db skip` recognize the legacy names either way.
- The installation guide is rewritten around `cctally setup`.

## [1.0.0] - 2026-05-06

### Added
- Initial public release of cctally.
