# Codexx

<img src="assets/codexx.svg" width="96" height="96" alt="Codexx icon">

Codexx is a local launcher for multiple ChatGPT accounts in Codex CLI. Automatic mode keeps the native Codex terminal open and reconnects a quota-failed conversation under another connected account. Each account has its own login and conversation state; configuration and capabilities can be shared across accounts. Multiple terminal sessions can use different accounts simultaneously. No external router, API key, or Python packages are required. The local bridge forwards terminal protocol messages to the official Codex app-server; model traffic goes directly from Codex to OpenAI.

> **Experimental:** simulated failover and offline compatibility checks pass. Live quota failover has not yet been validated.

**Start here:** [Step-by-step installation](#install) · [CLI](#cli-start-your-first-chat) · [Desktop](#set-up-native-codex-desktop-macos) · [Updates](#bundle-installation-and-safe-updates)

## Use `codexx` as your daily CLI

`codexx` opens the native Codex terminal with automatic account routing. It uses the
same installed `codex-accounts`, accounts, histories, routing policy and defaults.
Install the versioned bundle, retaining existing accounts and running services:

```sh
python3 install.py
codexx
codexx "Fix the failing tests"
codexx resume
codexx resume --last
codexx fork SESSION_ID
```

When the managed Desktop router is reachable, `codexx` connects to that existing
process directly. No Desktop window or SSH connection is needed. Otherwise it
starts a private local router on demand; subsequent terminals reuse it. Closing a
terminal disconnects it and leaves the router and its chats running. The terminal
keeps native flags and the native resume/fork picker, with history combined across
connected accounts. A per-terminal notification filter keeps other clients' chats
and approvals out of the terminal. Separate `codex-accounts auto` processes share
the account data and routing policy but keep their own backends; they are not
reattached to the shared router. Close such a terminal before resuming its chat.

Administration is under a separate namespace:

```sh
codexx accounts setup                  # Open the account management web app
codexx accounts add work
codexx accounts status
codexx accounts overview --history
codexx accounts routing --threshold 95
codexx accounts update                # Also updates the bundle after migration
```

`codexx exec` (including `exec resume`/`exec fork`) and `codexx review` run native
Codex with a selected account and shared defaults. They preserve stdin, stdout,
stderr and exit codes. They also use the launcher's preserved Codex package,
keeping tool helpers available if an update removes the original installation.
These native non-interactive commands do **not** support
the remote transport used for automatic failover, so they use one account per
invocation and return the native failure if that account runs out. Set
`CODEXX_ACCOUNT=work codexx exec ...` to choose an account. Exec resume resolves
saved session ownership across account homes, including `--last`.

Other native utility commands, including `login`, `logout`, `mcp`, `plugin`,
`completion`, `agents`, and history-management commands, pass directly to
Codex using its normal environment/home. They do not implicitly operate on every
switcher account. Use `accounts add/login` for switcher authentication and
`accounts shared` for capability sharing. Explicit `--remote` or local-model flags
also pass directly to Codex. `codexx --help` shows the installed Codex help and the
account entry points; `codexx --version` shows both versions.

`codexx update` manages Codexx. Use `codexx native update` for the official Codex
CLI updater, or `codexx native COMMAND` to bypass the wrapper explicitly.

### Bundle installation and safe updates

```sh
codexx update                         # Install the latest stable release
codexx update --check                 # Check without installing
codexx update --from-repo /path/to/codexx
codexx update policy auto             # Default: background checks once an hour
codexx update policy notify           # Check without installing
codexx update policy off
codexx update all sessions            # Request adoption of the installed engine
codexx update status --json           # Inspect current/pending/legacy sessions
codexx update --rollback              # Restore the previous complete bundle
```

The public entry points remain `~/.local/bin/codexx` and
`~/.local/bin/codex-accounts`. Immutable generations containing the wrapper,
engine, and updater live in `~/.local/bin/.codexx/versions/`. The `current` symlink
switches atomically only after syntax and startup checks pass for the entire
bundle. A `previous` pointer supports rollback. Account data remains under
`~/.local/share/codex-accounts`; original standalone entry points are retained as
`.codexx.pre-bundle` and `.codex-accounts.pre-bundle` during migration. No service
definitions are changed and installation never restarts services.
On the first migration there is no previous bundle yet; `--rollback` becomes
available after a second distinct bundle has been installed. The original
standalone files remain available in the migration backups.

Automatic updates follow published releases, not unreviewed Git commits. The
release publisher verifies the committed bundle and publishes its source archive
and SHA-256 checksum together. Downloads verify the checksum and GitHub artifact
digest when supplied. This trusts the repository and HTTPS; it is not independent
release signing. Offline, invalid, incomplete, or incompatible bundle-format
updates retain the working installation. Background checks do not block model
work. Checks occur on launch and while managed automatic sessions run; there is
no separate updater daemon. Rollback sets the policy to `notify`.

For local development, `git pull` followed by `codexx update --from-repo PATH`
installs the current checkout (including intentional uncommitted changes). Run the
test suite before installing local changes. Regular `install.py` also installs a
new generation. `--codexx-only` retains the installed engine; on legacy installs
it only adds the wrapper and does not enable bundle updates.

`update all sessions` requests adoption of the **already installed launcher
engine**. It never stops a process. Compatible managed chats adopt between turns,
after pending requests and approvals finish, with their backend processes, sockets,
history and locks retained. Status reports `current`, `pending_safe_point`,
`restart_required` for legacy/manual sessions, or `incompatible_restart_required`.
Older processes without the live loader cannot acquire it in place: leave their
work running and reopen them individually when convenient. A request does not
claim that every session has already updated.

This does not hot-swap the official Codex executable, its loaded configuration,
or an open terminal's wrapper code. New invocations use the current bundle; running
native backends keep their original preserved tool runtime. The existing
`codex-accounts update` entry point updates the entire bundle after migration.

## Set up native Codex Desktop (macOS)

Complete [installation](#install) first. Desktop integration is optional and currently packaged for **macOS only**. The account web app also currently requires macOS; Linux users can add accounts in the CLI.

1. Install and open the desktop app using [OpenAI's desktop installation page](https://learn.chatgpt.com/docs/app), then sign in. Codexx does not install the desktop app for you.
2. In Terminal, run `codexx accounts setup`. This opens the local account management web app.
3. Choose **Add account** and finish ChatGPT sign-in in your browser. Repeat for additional accounts. If your accounts are already listed, reuse them.
4. Under **Desktop connection**, click **Connect**. Wait for the local connection to report ready. Existing running services and accounts are retained.
5. In the desktop app, open **Settings → Connections → SSH → Add**. Use the hostname shown by the web app, normally `codex-auto`. Leave port and identity blank; the generated SSH configuration supplies them. If that connection is already present, select it instead of adding a duplicate.
6. Add a project folder **on that connection**, then start a new Codex chat in that project. An ordinary local project does not use Codexx routing. The names of desktop settings can vary by app version; the app must support SSH connections.
7. Return to the web app to see the chat and account activity. You can also check the local services with `codexx accounts desktop status`.

**Ready means the local services are available.** You still need to select the connection and its project in Desktop. Existing chats keep their original connection; they are not moved automatically. You can close the setup page after connecting.

The page shows account usage, searchable chats with their account and observed state, and an activity feed with automatic switches. Activity refreshes every 10 seconds; quota refreshes every minute while the page is open. Usage snapshots are retained locally for 30 days (latest 100 displayed), and the feed displays the latest 200 recorded events. Quota snapshots are account totals, not per-chat billing. Chats outside the launcher/managed Desktop connection are not monitored. PID reuse is excluded from connected-chat indicators.

The page also shows setup progress and local connection readiness. It never claims Desktop is linked just because the local services are ready. Codex Desktop still requires its own connection/project selection; there is no supported automatic approval API. Existing chats stay on their current connection.

Setup creates a dedicated localhost SSH key and alias and installs two user LaunchAgents. They start when you log in and restart after a crash. Re-running setup preserves accounts, histories, permissions, keys, and running services. No admin password, macOS Remote Login, external server, or Python packages are needed. Closing the page does not stop your chats. Its local web server exits after 30 minutes without page activity.

For headless use or detailed setup diagnostics, run `codex-accounts setup --terminal`.

```sh
codex-accounts add work             # Browser login for another account
codex-accounts overview --history   # Account usage and chat assignments
codex-accounts desktop status       # Check router and SSH readiness
codex-accounts desktop stop         # Stop managed services and active router work
codex-accounts desktop start        # Start services again
```

To install and open the account management web app, use `python3 install.py --setup`. You can also open it later with `codexx accounts setup`; connecting Desktop is optional. Use `setup --account LABEL` to select the initial account explicitly or `--port NUMBER` for a fixed localhost port. To change an existing setup's account or port, stop its services first, then rerun setup with the desired option. `desktop restart` loads updated router code and interrupts active router work; do it between turns. Updates and reinstalls preserve credentials and configuration. After a reboot, log in to start the services, then let Desktop reconnect.

Generated configuration, keys, logs, and settings live under `~/.local/share/codex-accounts/desktop`. Setup adds one Include line to `~/.ssh/config` and retains a backup before its first change. It does not replace existing SSH hosts or native Codex daemons. Only macOS onboarding is packaged today; Linux CLI and manual router usage remain available.

## Continue a chat you already have

```sh
codex-accounts continue
```

Choose a chat by title and project in the full-screen picker. Type to search instantly, use arrow keys to move, and press Enter to open. Tab cycles between this project, all projects, and launcher chats; Esc cancels. If the current directory has chats, the picker starts there. Compact columns show account, project, and date; the selected chat’s details appear below. Terminals without curses support fall back to a numbered picker. The picker includes launcher chats and locally saved chats in `~/.codex`, including local Desktop and ordinary CLI chats. It checks live limits, keeps the current account when available, and selects another account when the current one is blocked. Before opening the terminal it shows the account email, quota, and automatic-switching status.

```sh
codex-accounts continue billing          # Search by title or project
codex-accounts continue --list           # Browse without starting anything
codex-accounts continue billing --account codex2
```

An existing local chat is imported as a separate terminal copy. Its original stays in Desktop or the ordinary CLI; later messages do not sync between copies. After import the picker prefers the launcher copy so it does not repeatedly import the original. Stop/close the original session before importing; Codex's native writer lock may prevent import while the original remains loaded. Import never copies credentials or changes capability sharing. Shared capabilities, launcher defaults and the chat's saved settings apply as described below.

This does **not** switch the account of an in-place Desktop chat. The Codex Auto SSH connection provides a separate managed backend for native Desktop. A menu-bar account picker is not implemented. Cloud-only ChatGPT chats are not imported.

If limits cannot be verified, the launcher asks you to check status or choose `--account NAME` explicitly instead of silently treating an unknown account as available. Known blocked accounts are rejected. `--source-home PATH` searches another existing Codex home; `--json` lists matches without starting or importing a chat.

## Account and conversation overview

```sh
codex-accounts overview
codex-accounts overview rence --history
codex-accounts overview --account codex2
```

Shows current connected emails and live quota, saved conversations per account, open launcher sessions, project locations, and recorded account usage. `--history` includes timestamped opens, observed turn starts, imports, and account moves. `--include-local` adds local/Desktop chats whose account history is unknown; `--json` provides structured output, and `--limit N` changes the default 20-chat limit. It is also available in the no-argument menu.

Starting in v0.7.0, activity is stored in the private `activity.sqlite` database and survives process exit and updates. Restart older launcher sessions to enable recording. A stored history file indicates its current location, not which account executed all its old turns. Imported history is not retroactively attributed to the destination account. A move records an assignment; an observed turn start records execution under that account. Exact per-chat quota consumption and activity outside the managed router are unavailable. Emails shown identify accounts currently connected to each label; the audit tracks labels. New manual CLI sessions do not expose a reliable chat ID, so their launches are retained separately in JSON rather than guessed.

Since v0.7.1, temporary helper threads cannot replace the main chat’s tracking or reconnect settings. Unmatched sessions from older launchers appear separately as unverified tracking. Exit and reopen those sessions with `codex-accounts continue` when convenient; updating does not replace code already loaded in running processes.

## Install

Choose **CLI only** (macOS or Linux), **Desktop** (macOS), or both. They share the same accounts and local data. CLI use does not require the desktop app or its Connect button. Native Windows installation is not packaged.

### 1. Check prerequisites

Open Terminal. You need Python 3.9+, Git, and the official Codex CLI:

```sh
python3 --version
git --version
codex --version
```

If a command is missing, install it before continuing. On macOS with Homebrew, `brew install python git` supplies Python and Git. With Node.js/npm installed, install Codex using:

```sh
npm install -g @openai/codex@latest
codex --version
```

See [OpenAI's CLI documentation](https://learn.chatgpt.com/docs/codex/cli) and [official npm installation example](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex). Desktop users need the CLI too, because it runs the local backend. Codexx requires Codex's experimental `--remote unix://` and app-server interfaces; this release was locally checked with Codex 0.160.1.

### 2. Download and install Codexx

For the published v0.13.0 release:

```sh
git clone --branch v0.13.0 --depth 1 https://github.com/JoRo-Code/codexx.git
cd codexx
python3 install.py
```

Alternatively, download `codexx-0.13.0.tar.gz` from [v0.13.0 Releases](https://github.com/JoRo-Code/codexx/releases/tag/v0.13.0), extract it, open Terminal in the extracted folder, and run `python3 install.py`. Downloads do not require a GitHub account. The installer prints the installed bundle and entry points.

Already using `codex-accounts`? Run the same installer once. It retains accounts, history, settings and running services under `~/.local/share/codex-accounts`. You do not need to sign in again or restart existing chats. The old command remains supported. Already using a versioned Codexx installation? Use `codexx update` instead.

### 3. Make the command available

Run this in your current terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
codexx --bundle-version
```

It should print `0.13.0` (or a later installed release). To retain the PATH setting in new terminals, add the export line once to `~/.zshrc` for zsh (the macOS default), or `~/.bashrc` for Bash. The installer deliberately leaves your shell configuration alone.

### 4. Connect your accounts

On **macOS**, open the web app:

```sh
codexx accounts setup
```

Click **Add account**, finish ChatGPT sign-in, and wait for the account to appear. Repeat for each account you want to use. CLI-only users can leave **Desktop connection → Connect** untouched.

On **Linux**, or if you prefer terminal commands, add each account directly:

```sh
codexx accounts add personal
codexx accounts add work
```

Use distinct labels and select the intended ChatGPT account during each sign-in. Skip this step for accounts already connected. Confirm the result:

```sh
codexx accounts status
```

### CLI: start your first chat

Change into your own project directory (replace the example path), then start:

```sh
cd /path/to/your/project
codexx
```

You now have the native Codex terminal with automatic account routing. To continue later, run `codexx resume` or `codexx resume --last`. Use `codexx accounts setup` on macOS to reopen account usage and chat activity. `exec` and `review` use a single account per invocation; automatic failover applies to interactive chats.

### Desktop: finish connecting

Follow [Set up native Codex Desktop](#set-up-native-codex-desktop-macos) above to connect the app to the same accounts. You can keep CLI chats running while setting this up.

### Keep it updated

```sh
codexx update
codexx update all sessions
codexx update status --json
```

The first command installs the latest published bundle. The second asks compatible running sessions to adopt the installed engine when idle; it never forces a restart. Legacy sessions may report `restart_required`: leave their work running and reopen them individually when convenient. New terminal invocations use the installed bundle. An already-open setup page must be reopened with `codexx accounts setup` to show new UI changes.

Automatic release checks are enabled by default. See [safe updates](#bundle-installation-and-safe-updates) for update policy and rollback.

### If something does not work

| Symptom | What to do |
| --- | --- |
| `codexx: command not found` | Run the PATH export in step 3, or try `~/.local/bin/codexx --bundle-version`. |
| Installer cannot find `codex` | Install the official CLI, then ensure `codex --version` works in this terminal. |
| No connected accounts | Run `codexx accounts status`, then add an account using step 4. |
| Setup does not open on Linux | The web app is currently macOS-only. Use `codexx accounts add NAME` and `codexx accounts status`. |
| Desktop shows no routed chat | Check `codexx accounts desktop status` and select a project on the `codex-auto` SSH connection. Local readiness alone does not link Desktop. |
| Existing setup page still looks old | Open a new one using `codexx accounts setup`; reloading the old page uses its existing server. |

## Update without reinstalling

```sh
codex-accounts update                  # Download and replace the installed CLI
codex-accounts update --check          # Check without installing
codex-accounts update policy           # Show the update policy
codex-accounts update policy auto      # Automatic updates (default)
codex-accounts update policy notify    # Report availability without installing
codex-accounts update policy off       # Only update when explicitly requested
codex-accounts update --rollback       # Restore the previous executable
```

Installed copies check GitHub for stable releases when launched, at most once an hour. Available updates are verified, installed, and used for the requested command. There is no update daemon; an already-open conversation is never restarted. Offline update failures do not prevent normal CLI use. `--help` and `--version` do not check for updates. Automatic updates are limited to copies registered by `install.py`, not source checkouts.

Versioned installations update the complete bundle as described above. Legacy standalone installations download the standalone executable and SHA-256 checksum from this repository's GitHub Releases. The updater verifies the checksum, GitHub's artifact digest when supplied, the embedded version, and a startup check before atomically replacing the executable. This trusts this GitHub repository and HTTPS; checksums are integrity checks, not independent release signatures. A backup supports rollback. Rollback sets the policy to `notify` to avoid immediately reinstalling the same update. Updates do not downgrade; `--prerelease` explicitly includes preview releases for a manual check or update.

Connected accounts, cached credentials, history, and routing settings remain in the data directory. `updates.json` stores the check time and update policy. Compatible running routers and automatic terminals notice installed file updates within one second and adopt new launcher code between requests. Their sockets, backends, credentials, history, model settings and approvals remain in place. Each chat adopts independently: an active turn or pending approval keeps its loaded code until it completes. Invalid updates retain the loaded generation; incompatible live-state ABI changes are deferred and logged. Rollback is adopted by the same mechanism.

**One-time bootstrap for versions before 0.4.0:** those versions do not contain an updater. Run `git pull` and `python3 install.py` once. After that, use `codex-accounts update`; no new clone or reinstall is needed. A source checkout is updated with Git rather than overwritten by the executable updater.

### Live updates in long-running chats

Managed routers and automatic terminals check the installed executable while they run. Hourly release checks also run in a separate process according to `update policy auto|notify|off`, so model work does not wait on GitHub. Live adoption retains the process, sockets, actor queues, pending requests, history locks and native backend PIDs. It runs only outside active turns, outstanding backend messages, requests and approvals. New chats use the latest compatible generation immediately; active chats adopt when ready. This updates launcher routing/transport code, not the separately installed Codex CLI or a backend’s loaded configuration.

The stable supervisor and live-state ABI form the compatibility boundary. Releases preserving `LIVE_RUNTIME_ABI` must preserve live object fields; breaking state/supervisor changes require a new process and are deferred instead of interrupting work. Loader failures are logged in the managed router log.

**One-time transition:** processes started before the live loader existed cannot discover it. Let their active work finish and adopt the loader at their next natural start, or restart an idle router once. Thereafter ordinary compatible updates require no manual restart.

## Connect accounts

```sh
codex-accounts add personal
codex-accounts add work
codex-accounts list
```

Each `add` runs the official Codex browser login. Select the correct ChatGPT account in the browser; signing into the same account twice does not give separate quotas. If browser login is inconvenient, use `add NAME --device-auth` (requires device login enabled for your account). Use `login NAME` to reconnect later.

`list` asks each account’s Codex backend for its cached email and plan, so you can identify which ChatGPT login each label represents. It does not refresh tokens or verify remaining quota. If an identity cannot be read, that row says `identity unavailable`; other accounts still appear.

Credentials remain local under `~/.local/share/codex-accounts/accounts/NAME/auth.json`. They are stored with private permissions, and Codex performs its own token refresh. Account names are labels you choose; they do not verify the identity you select in the browser. `status NAME` fetches live usage limits and shows launcher sessions for that account; `status` without a name shows all accounts.

## Account dashboard

```sh
codex-accounts status               # Live snapshot of all accounts
codex-accounts status --watch       # Keep open; refresh every 30 seconds
codex-accounts status codex1        # One account
codex-accounts status --json        # Structured snapshot for your own tools
```

Each account shows:

- Its label, signed-in email, and cached plan.
- Quota windows returned by Codex, including remaining/used percentages and reset times in your local timezone.
- Backend-reported usage blocks, spending limits, workspace credits, and earned resets when available.
- Launcher sessions: project path, conversation ID, title, model, process ID, and time open.
- Automatic-session activity: starting, working, waiting for input, idle, failed, or switching accounts.
- Account routing cooldowns.

`--watch --interval 60` changes the refresh interval (minimum 10 seconds). Press Ctrl-C to close the dashboard; running conversations keep going. If a lookup fails, that account shows limits unavailable while the others remain visible. Unknown limits are never reported as zero usage. Displayed reset times do not establish that backend access has recovered. The dashboard does not redeem resets, buy credits, send notifications, or change routing state.

Limits come from OpenAI's account endpoint and can reflect usage outside the launcher. Session locations cover only launcher processes on this computer; desktop sessions and ordinary `codex` processes are not inventoried. Manual mode reports the CLI as open because it cannot observe turn activity. Restart older launcher processes to get the new project/title/activity tracking. Workspace credit balances can be shared across accounts and should not be summed as independent allowances.

## Daily use: automatic mode

To prefer another account’s ordinary quota before using credits, enable a rotation threshold:

```sh
codex-accounts routing --threshold 95
codex-accounts routing                 # Show the current policy
codex-accounts routing --off           # Restore quota-error failover only
```

With this policy enabled, each root chat checks live ordinary usage before forwarding a new `turn/start`. If either quota window is at or above the threshold, or ordinary usage is blocked, the launcher resumes the chat on another account with confirmed usage below the threshold and forwards the original input once. Available credits do not qualify an account as a preferred destination. If the current quota cannot be verified or no account qualifies, the current account continues normally, including credit use. Reactive failover also prefers ordinary quota but can fall back to an account with credits. Settings are shared by automatic terminals and the managed Desktop router; manual `run` chats are unaffected. Threshold changes are read before each new turn. Once the live loader is running, installing a compatible update needs no router or terminal restart.

Rotation happens between user turns. It does not interrupt a running turn, reserve quota against concurrent chats, or prevent an ongoing turn from crossing the threshold. Helper/subagent histories are not automatically moved. Activity records `quota_rotation` and `auto_switched`.

```sh
cd /path/to/project
codex-accounts auto
```

Open as many terminals as you need and run the same command. The launcher chooses a connected account using active-session counts and least-recent selection, skipping accounts in cooldown. You keep the normal Codex terminal UI, approvals, and tool interactions.

```sh
codex-accounts auto --account personal    # Choose the starting account
codex-accounts auto --resume SESSION_ID  # Continue an existing saved conversation
codex-accounts auto --cd /path/to/project --prompt "Fix the login page"
codex-accounts sessions
codex-accounts list                      # Labels, emails, plans, running sessions, cooldowns
```

When Codex reports that a turn has completed with a structured `usageLimitExceeded` or `rateLimitExceeded` error, automatic mode:

1. Marks that account unavailable for the reported exhausted quota window, or briefly cools it down when no reset time is known (five minutes for usage limits, one minute for rate limits).
2. Stops that conversation's backend so its tools and history writer have stopped.
3. Copies its saved history to another connected account and remembers the new owner.
4. Reconnects the same terminal to a fresh official Codex backend under that account.
5. Starts a continuation turn instructing Codex to inspect preserved work and avoid repeating completed side effects.

You do not need to close the terminal, find the session ID, move the history, or type another prompt. The native UI may briefly show the original quota error before the continuation starts. Other conversations have their own backends and continue independently. Shared cooldowns also guide new launches.

Exiting the terminal also stops this launcher's backend. Codex may print a generic remote-mode message saying work continues and suggesting a temporary socket reconnect command; that does not apply to this launcher. Use the `codex-accounts auto --resume SESSION_ID` command printed afterward to reopen a saved chat. Starting `auto --account NAME` again creates a new chat.

Only structured quota errors trigger this behavior, after Codex's own retries have ended. Generic errors, authentication failures, and subagent notifications do not trigger account switching. The model and permission choices are carried forward. This is a new continuation turn with the existing history, not resumption of an interrupted network response. The launcher does not rerun tool commands itself; model continuation cannot guarantee exactly-once external side effects.

Each account is tried at most once per failed-turn chain. If every connected account is exhausted, the terminal stays open with the failure and saved history; it does not spin or wait indefinitely. You can submit a new turn later, resume later, or connect another account. A cooldown expiring only makes an account eligible for another attempt; it does not establish that quota recovered.

## Manual mode

Run `codex-accounts` for an interactive menu, or explicitly pin a terminal to an account:

```sh
codex-accounts run personal
codex-accounts run work
codex-accounts resume SESSION_ID
```

`run` without an account offers a picker. Manual `resume` uses the current owner and returns to the saved project directory. Session IDs accept unambiguous prefixes. `--cd` and `--model` can override saved values. Use `auto --resume SESSION_ID` to enable failover while resuming.

Multiple sessions on the same account share that account's quota. Manual mode uses `--no-daemon`; automatic mode connects to a dedicated private Unix socket. Both avoid the shared default server. Ambient API keys and known alternate authentication variables are removed from child processes, and the launcher requires ChatGPT authentication with the OpenAI provider.

## Optional manual move

1. Exit that CLI conversation normally. Moving does not interrupt or retry a running turn.
2. Find its ID with `codex-accounts sessions`.
3. Move and resume:

```sh
codex-accounts switch SESSION_ID --to work
codex-accounts resume SESSION_ID
```

The launcher copies the complete saved legacy or paginated JSONL history to the destination account, records the new owner, and retains the original. Moving back replaces the destination's older copy after backing it up. Existing project files stay in place. The target account must already be connected.

Use the launcher's `resume` after moving, rather than a native `/resume` picker inside another running Codex session. Native pickers may still expose retained old copies. The launcher tracks only processes launched through it; it cannot detect a session opened outside it or a different session selected through native `/resume`. Close those before moving. A known running conversation blocks its own move; other resumed conversations can continue. A newly started CLI session has no registered ID yet, so moving from that account conservatively requires closing its new-session processes first.

History transfer is a local, version-sensitive mechanism, not a built-in OpenAI account-switch feature. Paginated history stays paginated: the destination’s derived history index is cleared only for this chat, and Codex rebuilds it from the copied log on resume. Native Codex writer locks prevent copying active histories. Unknown database versions or schemas fail before replacing history. Subagent sessions are separate histories and are not moved automatically. Automatic mode uses the same history transfer after a structured quota failure. It checks remaining quota before user turns when a routing threshold is enabled; it does not automatically migrate subagent histories.

## Configuration and scope

To use the same configuration and capabilities with every account:

```sh
codex-accounts shared sync                         # Use ~/.codex as the common home
codex-accounts shared status --json                # Inspect linked and pending resources
codex-accounts shared sync --source-home /private/common-codex
codex-accounts shared off                          # Restore original account resources
```

Sharing is opt-in and persistent. Once enabled, existing profiles are migrated and every new account/backend is prepared before launch, including manual use, automatic failover and managed Desktop. The account's `CODEX_HOME` and `auth.json` remain separate. No login tokens, MCP OAuth stores, model-availability caches, conversation histories, databases or writer locks are copied. Account/workspace-specific app grants and organization requirements still apply.

The canonical `config.toml` is shared using a filesystem link, so plugin enablement, MCP definitions, feature flags, hooks, model preferences, project settings and Desktop configuration have one source. Named `*.config.toml` profiles and `AGENTS.md`/`hooks.json` are also linked. Shared directories include skills, installed plugin resources, rules, custom agents/prompts/hooks, automation definitions, pets, packages, browser state and Computer Use state. Existing Chrome registration metadata and the installation ID are reused when available; this does not install a native host or grant browser/OS permissions. Source files remain in the chosen canonical home.

Generated `.tmp/bundled-marketplaces` catalogs remain separate for each profile. Desktop generates different catalogs for local and SSH hosts; sharing this directory lets a reduced SSH catalog replace the local catalog. Version 0.12.1 restores links created by 0.12.0 from their existing backups on the next `shared sync` or account launch. Interrupted restores can be retried, and external edits are preserved.

Existing account directories contribute missing installed resources to the common directories. Canonical files win on conflicts; original account files/directories are retained privately under `shared-backups`. The canonical configuration wins over old per-account configuration. Native Codex settings writes preserve the config link and reach other accounts. Interrupted migrations and restores can be retried, and externally detached/edited paths stop synchronization rather than being overwritten. `shared off` restores the original account resources and leaves the common home available. Turn sharing off before selecting a different common home.

Running Codex backends retain their loaded configuration. Reopen a chat/backend to load newly enabled integrations. Compatible launcher updates are adopted automatically between requests without restarting the managed Desktop router or its backends. Chrome was verified through the managed Desktop SSH adapter using the official browser runtime: extension discovery, open-tab listing, navigation and a live accessibility snapshot. This validates that browser connection; shared files alone do not establish complete Desktop/browser/automation parity.

For packaged Codex installations (`codex-package.json`, layout version 1), the launcher runs a private snapshot of the complete native package under `native-runtimes`. This keeps the matching code-mode host, bundled shell and resources available if Homebrew removes the installed release during a running chat. New launches and replacement account backends select the currently installed package; existing processes retain their original package. Snapshots are retained because old backends may still need them. Custom/unpackaged executables keep their existing launch behavior.

If a backend started before this protection reports `failed to spawn code-mode host ... No such file or directory`, exit that terminal session between turns and use `codex-accounts auto --resume SESSION_ID` to start a fresh backend. Do not reuse a stopped launcher's temporary `--remote .../tui.sock` command.

Without sharing, account homes start fresh and per-account configuration remains in `~/.local/share/codex-accounts/accounts/NAME/config.toml`. Project-level settings continue to load normally. The launcher forces file-based ChatGPT credentials and the OpenAI provider in either mode. Managed authentication restrictions still apply.

### Shared permissions and model defaults

Set these once for every existing and future launcher account:

```sh
codex-accounts permissions yolo
codex-accounts defaults --model gpt-6-astra --effort medium
codex-accounts permissions                 # Show permission mode
codex-accounts defaults                    # Show model defaults
```

YOLO sets `approval_policy="never"` and `sandbox_mode="danger-full-access"`: unrestricted filesystem and network access without command approval prompts. It is opt-in. These shared settings apply to manual starts, resumes, the automatic terminal, and replacement backends after account switching. Explicit `--model` and in-chat choices override model defaults; automatic continuation preserves the current chat’s model and permission choices. Organization requirements still apply, and this does not grant OS permissions or authenticate plugins.

Restart existing launcher sessions to apply new defaults. Settings are stored separately from the executable in `permissions.json` and `defaults.json` under the launcher data root, survive updates/reinstallation, and apply to accounts added later. These command-line overrides take precedence over the shared or per-account configuration. Use `codex-accounts permissions default` and `codex-accounts defaults --clear` to return to the configured Codex defaults.

Run launcher commands in your shell, not as a prompt inside another Codex chat. An outer Codex session has its own permissions and may ask for approval before it can launch the command.

This controls the CLI only. It does not change the desktop app, IDE extension, or existing ordinary `codex` processes. Avoid editing the same project files concurrently unless you intend to coordinate that work.

`CODEX_ACCOUNTS_HOME` overrides launcher storage. `CODEX_ACCOUNTS_BINARY` selects a Codex executable. Uninstall the command by removing `~/.local/bin/codex-accounts`; account data remains in the storage directory until you separately remove it.

## Validation

```sh
python3 test_launcher.py
python3 test_shared.py
python3 test_auto.py
python3 test_status.py
python3 test_update.py
python3 test_live_update.py
python3 test_native_history.py
python3 test_native_paginated_history.py
python3 test_continue.py
python3 test_overview.py
python3 test_native_shared.py
```

Tests cover isolated credentials/environment, concurrent account locks, duplicate-resume protection, active-session move protection, complete history preservation and round trips, unsupported-format rejection, argument forwarding, and account-name validation. The paginated native check verifies A → B → A migration with a stale destination index, two preserved turns, pagination, and native writer locks. The legacy offline native check verifies that the installed Codex app server discovers the moved history and reads its user and assistant messages, without making a model request. Additional tests simulate quota failover, exhaustion of all accounts, concurrent-session routing, cooldowns, preservation of model/approval settings, isolation of subagent events, pending RPC handling, and WebSocket framing. The real native terminal was also connected through the bridge up to its authentication check. Live identity and quota retrieval have been checked with connected accounts. Model requests and real quota failover are not exercised by the tests. Automatic failover is implemented but has not yet been validated against a live account quota failure.

Shared-capability unit tests cover credential/history isolation, resource union and conflicts, future accounts, concurrent starts, interrupted migration recovery, external edit protection, and rollback. The optional `test_native_shared.py` verifies common config, skill/MCP discovery and native settings writes with real Codex backends and a local fixture MCP server. It sends no model requests. The native router test also checks that shared MCP tools remain available after injected quota failover.

Official building blocks: [authentication](https://learn.chatgpt.com/docs/auth) and [configuration/state locations](https://learn.chatgpt.com/docs/config-file/config-advanced).

## Development and license

The launcher uses only the Python standard library. Unit tests run without a Codex installation; the optional native history tests require Codex. GitHub Actions runs the unit tests on macOS and Linux.

Report bugs in [GitHub Issues](https://github.com/JoRo-Code/codexx/issues). Include the CLI versions and error text, but never attach `auth.json`, tokens, or private conversation histories.

Released under the [MIT license](LICENSE). This is an independent project, not an official OpenAI product.

### Publishing a new version

Update `VERSION`, commit the changes, run the tests, and push the matching version tag. Then publish with:

```sh
python3 scripts/publish_release.py --notes-file /path/to/release-notes.md
```

The publisher uploads a standalone CLI, its checksum, and a source archive to a draft release, then publishes it only after all assets are present. Use `--prerelease` for preview releases; automatic updaters ignore previews. Keep CLI/data compatibility so rollback remains possible. The GitHub CLI must be authenticated with permission to publish releases.

## Experimental native Desktop / multi-session router

Version 0.8 adds a persistent local protocol router. Each loaded root chat owns a separate Codex app-server process, account, writer lock, and request queue. A quota failure migrates only that chat's saved history, then resumes its unfinished request on an eligible account. Other chat backends keep running. Disconnecting a client leaves its chats running until the router is stopped. This uses more memory than a single shared backend.

Start the router in a private, short-path directory:

```sh
codex-accounts serve --socket /tmp/codex-accounts-$UID/router.sock
```

Optionally add `--account codex2` to prefer that account for new chats. Resume requests retain their current account until a structured quota failure triggers rotation. Eligibility uses cached login presence and shared quota cooldowns; it is not a guarantee of current quota or valid credentials. When all alternatives are exhausted, the chat pauses with an error instead of retrying endlessly. After quota resets, retry the chat. Models and permissions use launcher defaults plus saved/client-selected settings.

Connect an independent protocol client using the native CLI:

```sh
codex app-server proxy --sock /tmp/codex-accounts-$UID/router.sock
```

Native terminal clients can connect with `codex --remote unix:///tmp/codex-accounts-$UID/router.sock`. The socket accepts both native newline-JSON proxy traffic and WebSockets, and is accessible only to your OS user. Do not expose it over a public network.

For Desktop, the SSH host's Codex entry point must forward its app-server/proxy requests to this socket. Merely adding an ordinary SSH host does **not** enable rotation. The macOS `setup` command installs this adapter and its background services; Desktop still requires a connection/project selection. Existing Desktop connections need reconnecting to use a changed adapter; do so between turns. Do not replace a live native daemon or its socket. Use `codex-accounts overview --history` to see account assignments and moves.

The native account indicator describes the router's primary control account, not every chat's execution account or combined quota. Manage authentication with `codex-accounts add/login`; authentication changes from connected protocol clients are rejected. Chat history lists combine connected accounts and deduplicate moved histories. Pending approval request IDs are isolated between backends and replayed after reconnect. Settings/plugins that are not thread-scoped still belong to the primary account's backend; full Desktop plugin/browser/automation parity has not been verified.

Validation includes native proxy connections, two chats, client reconnect, combined history, and an injected quota failure that migrates history through real Codex backends while another chat remains accessible. The injection sends no model request. Actual quota exhaustion and seamless native Desktop UI recovery after rotation still require a live test. Automatic continuation is a new turn and does not guarantee exactly-once tool side effects.

Stopping `serve` stops its managed backends and tools. Saved histories and account assignments remain available for a later start. Running `serve` directly does not install a launch-at-login service; use `setup` for managed startup on macOS. Run the optional compatibility test after upgrading Codex:

```sh
python3 test_native_router.py
python3 test_native_router.py --proactive
python3 test_native_router.py --live
```
