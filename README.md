# attobot

A persistent agent in a single `agent.py`.

One agent = one working directory (in production, one unix user's `$HOME`) + one process running `agent.py`. All agent state lives in `./agent/`.
The process is a loop that re-runs the LLM every time `messages.jsonl` changes.

## The loop

```
hash messages.jsonl
  if unchanged: sleep
  build system prompt + sum chars
  if over budget: stash_messages
  llm(<soul> + <harness> + <memory>, messages + [<life-tail>], tools)
  append assistant reply
  if tool_calls: run each via bg_run, append results
  else: automatic telegram text reply
```

That's `agent.py`. Channels, tools, and the backgrounding wrapper all live inline in the same file.

## Channels in

Three daemon threads append to `messages.jsonl`:

- **telegram** — `start_chat()` long-polls `getUpdates`. Inbound text → `{role:user, content:"[telegram <id>] …"}`. Only the `chat_id`/`thread_id` locked in `config.json` is accepted. Optional: no `telegram_token` in config → no chat channel; the agent wakes on triggers/mail only.
- **triggers** — `start_triggers()` scans `agent/triggers/*.json` every 30s; due ones append `{role:user, content:"<system-message>[trigger <name>] …</system-message>"}`. Three kinds: a **cron** — `{"next": <ts>, "repeat_s": <s?>, "message": "…"}` — fires on the clock (repeating ones reschedule, one-shots delete); a **watch** — `{"watch": "<path>", "repeat_s": <cooldown?>, "message": "…"}` — fires when the file's content changes; a **cmd** — `{"cmd": "<shell>", "repeat_s": <s?>}` — runs the command (60s timeout) and fires with its stdout (clipped), no output = no fire. Combined with `watch`, the cmd runs when the file changes but receives no stdin; commands that need context should read files themselves. Fires are queued and injected one at a time, only when the stream is idle (last line is a plain assistant reply, no tool_calls in flight); if the previous trigger got only an idle text reply, that pair is collapsed before the next one lands, so unactioned triggers don't pile up. Triggers named `subconscious-*` are additionally surfaced to the operator's Telegram.
- **mail** — `start_inbox()` polls `agent/mail_inbox/`. New files append `{role:user, content:"[mail from <unix-user>] <name>\n<preview>"}` and notify the operator via chat.

There is no mid-stream `role:system` — the only system message in a request is the system prompt itself. System-ish injections (triggers, bg completions, stash markers, the start banner) are user messages wrapped in `<system-message>…</system-message>`; the harness strips the wrapper when checking prefixes.

## Channel out

Text replies are sent to telegram automatically — except when the turn's inbound was a `<system-message>`-wrapped injection (trigger, bg completion, stash marker) and the turn did no tool work: those replies are muted (this is what makes an idle heartbeat reply free). `SEND_ATTACHMENT` sends files to telegram and rejects text-only sends.

## Tools

Declared in the `TOOLS` list in `agent.py`: `(NAME, fn, description, parameters)` per entry.

| name | what |
|---|---|
| `SEND_ATTACHMENT` | send a file to telegram (photo/voice/video/audio/document by extension; optional caption) |
| `READ_FILE` | file → line-numbered text; images → multimodal content blocks (when `multimodal_support=true`) |
| `WRITE_FILE` / `EDIT_FILE` | filesystem writes; `EDIT_FILE` has optional `replace_all` |
| `BASH` | run a shell command (returns Popen → streamed by `bg_run`) |
| `SEARCH` / `WEB_FETCH` | DuckDuckGo + markdownified page fetch |
| `STASH` | content-addressed save to `agent/blobs/<hash>`, returns `[stash <hash>]` |
| `STASH_MESSAGES` | collapse a line range of a messages.jsonl (default: own, middle half) into one `[stash <hash>]` line with an LLM summary |

Every tool call runs through `bg_run`; a tool returning a `subprocess.Popen` (or anything with `.pid` + `.communicate`) is streamed through it.

Tool results longer than `tool_output_limit` (5000 chars) are auto-clipped to `<head>\n... N chars truncated, [stash <hash>] ...\n<tail>`. The agent recovers the full content with `READ_FILE agent/blobs/<hash>`.

## Backgrounding

`bg_run` runs the tool in a thread with `tool_timeout` (30s). Finishes in time → inline (post-clip) result. Otherwise the work keeps running in the background:

- registers `agent/bg/<id>.json` (with pid if known)
- returns `[backgrounded bg/<id> (pid …); kill the pid to stop it]` to the assistant immediately
- an emitter thread waits for the worker to finish — however long that takes — then appends `[bg <id> done, tc:…] <result>` as a `<system-message>` user message and removes the json

The bg json holds the pid so the agent can kill a runaway task itself. Background work does not survive a process restart (the worker is a daemon thread); anything that must outlive the harness should detach itself (e.g. `nohup … &`).

## State

```
SOUL.md                       # the prompt template (copied into agent/ by setup.py)
agent.py                      # the harness, included verbatim in the system prompt
opt/
  tools/<name>.py             # optional capability tools (see Optional add-ons)
  providers/<name>.py         # alternative LLM providers
  subconscious/               # reviewer agent skeleton (soul + seeded trigger), copied out beside agent/
agent/
  SOUL.md                     # this agent's soul (copy of the template)
  MEMORY.md                   # memory index: one pointer line per memory
  memory/<name>.md            # memory bodies, read on demand via the index
  LIFE.md                     # append-only event log; tail rides as a trailing user message
  messages.jsonl              # canonical conversation, one JSON message per line
  config.json                 # telegram token/chat, api key, overrides
  tg_poll.offset              # telegram update_id cursor
  triggers/<name>.json        # crons (clock), watches (file change), cmds (computed)
  triggers/heartbeat.json     # auto-created at boot (not for subconscious dirs), 225s tick, backs off when idle up to 1h
  mail_inbox/                 # drop files here (delivered once, then moved to processed/)
  inbound/                    # files received over telegram
  bg/<id>.json                # in-flight background work
  tools/<name>.py             # opt-in tools (copied from opt/tools/ at first boot)
  providers/<name>.py         # opt-in provider (copied from opt/providers/ at first boot)
  blobs/<hash>                # content-addressed store
```

## System prompt

Built fresh every turn:

```
<soul>          agent/SOUL.md
<harness>       agent.py source
<subconscious>  one-liner, present only when a subconscious/ sibling dir exists
<memory>        MEMORY.md (middle-elided if > MEMORY_LIMIT)
```

The last `life_tail` lines of LIFE.md (prefixed with `[N bytes earlier]`) are not part of the system prompt — they ride along as a trailing `<system-message>` user message after the conversation, rebuilt every turn.

The agent sees its own harness. Modify `agent.py` and the agent's self-model updates next turn.

## Memory pressure

- Memory is two-tier: `MEMORY.md` is an always-in-context index (one pointer line per memory), bodies live in `agent/memory/` and are read on demand. `MEMORY.md > MEMORY_LIMIT` (10000) → middle is elided with a warning telling the agent to move detail into `agent/memory/` files.
- System prompt + life-tail + serialized messages, divided by 4 chars/token, > `context_tokens * 0.8` → `stash_messages` runs automatically; the middle half of `messages.jsonl` goes to a blob, replaced by a single `<system-message>`-wrapped user message holding `[stash <hash>]` plus an LLM summary (ranges snap past tool messages so tool-call blocks stay intact). The agent can `READ_FILE agent/blobs/<hash>` to recover.
- A user message whose content is `STASH_MESSAGE: <start> <end>` or `STASH_MESSAGE: all` (bare, or right after a `[trigger …]` prefix) is a **stash directive**: the loop intercepts it before calling the LLM, removes the directive line, stashes the range, and owes the agent a re-orientation turn. This is what the subconscious's `PRUNE` rides.

The 4-chars-per-token heuristic over-counts base64 image content — safe direction. `load_messages()` also heals the file on every read: malformed lines and incomplete tool-call blocks are dropped and the file is rewritten.

## Optional add-ons

Anything under `opt/` is opt-in via the `opt` field in `config.json` (a list of paths relative to `opt/`, no `.py` suffix):

```json
"opt": ["tools/ocr_image", "providers/anthropic"]
```

Each entry copies `opt/<path>.py` → `agent/<path>.py` at first boot. From then on, the agent owns its copy.

**Tools** in `agent/tools/` auto-register at startup. Built-in:
- `ocr_image` — RapidOCR + spatial ASCII layout, for text-only LLMs. Auto-included when `multimodal_support=false`. Requires `rapidocr-onnxruntime` + `opencv-python`.
- `nudge` (`NUDGE`) / `stash_messages` (`PRUNE`) — the subconscious's correction tools (see Subconscious); they act on the *sibling* `agent/` dir, so they only make sense in a subconscious's `opt` list.

**Providers** swap the `llm` function. Set `provider: "anthropic"` (auto-includes `providers/anthropic`) to use it. Built-in:
- `anthropic` — native `/v1/messages` translation. Reads `api_key` and `model` from `config.json` like the default provider.

## Subconscious

A second attobot that reviews the first. Same harness, different soul (`opt/subconscious/` — an agent-dir skeleton: soul + a pre-seeded watch job), no chat. It runs in the same unix user as the primary — it needs direct read/write into `agent/` — unlike peer agents, which get a user each.

```bash
python setup.py --subconscious ...   # copies opt/subconscious/ out beside agent/, reuses the api_key
python agent.py agent subconscious   # one command, one process per dir
```

For an existing install: `cp -r opt/subconscious . && echo '{"api_key": "sk-...", "opt": ["tools/nudge", "tools/stash_messages"]}' > subconscious/config.json` (the `opt` entries copy NUDGE/PRUNE into `subconscious/tools/` at first boot — the subconscious SOUL depends on them; `setup.py --subconscious` writes the same config).

A pre-seeded `selfwipe.json` trigger stashes the subconscious's own `messages.jsonl` to a single pointer every ~30min (only when it has grown past 20 lines) — the self-wipe its SOUL describes.

It wakes via its pre-seeded `primary.json` trigger — a watch+cmd on `agent/messages.jsonl` with a 450s cooldown whose cmd diffs the stream against a snapshot (`subconscious/.primary_snap`) and fires with the new lines as the trigger message, but only when more than 10 lines are new (no heartbeat: the harness skips heartbeat creation for dirs named `subconscious`). It corrects the primary via two opt tools (`opt/tools/nudge.py`, `opt/tools/stash_messages.py` — copy them into `subconscious/tools/`, where they register as `NUDGE` and `PRUNE`):

- `NUDGE` — writes a one-shot trigger `agent/triggers/subconscious-<name>.json`; the primary's trigger thread fires it as a `[trigger subconscious-<name>] <message>` injection on its next tick and, because of the `subconscious-` prefix, also surfaces it to the operator's Telegram.
- `PRUNE` — writes a one-shot trigger carrying a `STASH_MESSAGE: <start> <end>` directive; the primary's loop intercepts it and collapses that line range of `messages.jsonl` into a stashed summary pointer. Use on context rot or to refocus the primary.

Both act through the trigger-file bus — the subconscious never writes the primary's `messages.jsonl` directly, so the stream can't be corrupted.

## Run

```
pip install -r requirements.txt
python setup.py                            # prompts for token, auto-discovers chat_id, prompts for api_key
python agent.py
```

`setup.py` accepts CLI args for non-interactive use (e.g. an HR-style agent spawning new agents):

```
python setup.py --token 123:abc --chat -1001234567 --api-key sk-... [--thread 42] [--systemd]
```

Required config (`agent/config.json`) is created by `setup.py`. It validates `GET /getMe` and refuses to proceed if the bot's privacy mode is on or `can_join_groups` is off.

## Deploy

One agent per unix user: give the agent its own user, clone this repo into their `$HOME`, run `setup.py` and `agent.py` from there. The agent owns its copy of the harness; editing it affects no other agent.

On macOS or for quick testing, just run `python agent.py` (use `tmux` to keep it alive across logout).

On Linux, run `setup.py --systemd` as the dedicated user to emit a systemd unit + install instructions:

```bash
python setup.py --systemd
# wrote agent/config.json
# wrote attobot.service
#
# Install (user service, no sudo):
#   mkdir -p ~/.config/systemd/user
#   cp attobot.service ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   loginctl enable-linger $USER          # so it survives logout
#   systemctl --user enable --now attobot
#   journalctl --user -u attobot -f
```

Default: `deepseek-v4-pro` via `https://api.deepseek.com/v1`. Override `model` / `api_base` in `config.json` to point at any OpenAI-compatible endpoint, or set `provider: "anthropic"` to switch the request shape.

`python agent.py [agent_dir ...]` — the arg is the agent state folder (default `./agent`); same optional arg on `setup.py`. It must hold `config.json` and `SOUL.md` (`setup.py` creates both). Extra dirs each get their own process (`python agent.py agent subconscious` runs the pair; ctrl-C kills both; a child that dies is respawned after 10s and the death is logged to the primary's LIFE).

`agent/config.json` fields (only `api_key` is required — omit `telegram_token` for a chat-less agent; the rest fall back to sensible defaults baked into `agent.py`):

```jsonc
{
  "telegram_token": "...",         // optional — omit for no chat channel
  "telegram_chat_id": "...",       // required if telegram_token is set
  "telegram_thread_id": "...",     // optional, forum supergroup topic
  "api_key": "...",                // required, LLM provider key
  "model": "deepseek-v4-pro",
  "api_base": "https://api.deepseek.com/v1",
  "temperature": 1.0,
  "reasoning_effort": "medium",
  "context_tokens": 100000,
  "multimodal_support": false,
  "provider": "",                  // "" = openai-compat default; "anthropic" loads opt/providers/anthropic
  "opt": []                        // additional opt/ entries to copy in
}
```

Tunables with defaults in `CFG` (rarely worth changing, override in `config.json`): `life_tail`, `memory_limit`, `tool_timeout`, `trigger_tick`, `inbox_tick`, `inbox_preview`, `chat_msg_max`, `tool_output_limit`. `AGENT_DIR` / `BLOB_DIR` are in-source constants.

## Principles

1. **The agent is a loop.** One process, one file watch, one LLM call per change.
2. **The bus is the filesystem.** Channels in, channels out, scheduled jobs, background work, memory — all files. No daemon, no queue, no IPC.
3. **Opinionated cuts code.** Telegram is the chat. One operator, one chat. Default is DeepSeek V4 Pro via DeepSeek, but anything OpenAI-shape works out of the box and other shapes live in `opt/providers/`. No abstractions for things that aren't pluralized.
