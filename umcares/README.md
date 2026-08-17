# umcares

**umcares does not decide anything creative. It renders what a recipe says.**

    media on disk  ──►  umcares inspect  ──►  manifest + contact sheets
                                                      │
                                                      ▼
                                            AI reads and WRITES a recipe
                                        (scenes, narration, cards, subtitles)
                                                      │
                                                      ▼
                            umcares render  ──►  video  ──►  regenerate on edit

The split exists because the two halves fail differently. Judgement — which
clip shows the speaker, what the narration should say — is what a model is good
at. Arithmetic — where each clip starts, whether the music is still ducked when
the last sentence ends — is what it is bad at, and every timing bug this
pipeline has had came from someone doing that arithmetic by hand.

So the AI declares intent and never computes a timing. umcares measures real
durations from rendered audio and probed clips, resolves the timeline, and
produces the same cut every time from the same recipe.

```bash
# install: symlink into a dir already on PATH (matches the pr-cli convention)
ln -sfn "$PWD/bin/umcares" ~/.local/bin/umcares

umcares session          # create the 3-pane tmux layout
umcares auth --setup-key # key login, so ssh replaces the tmux fallback
umcares doctor           # preflight everything
umcares init             # create the folder tree
umcares premiere heal    # make Premiere controllable
```

No zshrc edit is needed if `~/.local/bin` is already on your PATH. The launcher
resolves its own symlink, so `.env` and the package are found from any working
directory.

### Why key auth matters

`auth --setup-key` installs the public key **through whatever transport already
works** — usually the logged-in tmux pane — so it never prompts for a password.
After that the CLI opens its own short-lived ssh connections:

| | 3 commands | push 61 KB |
|---|---|---|
| ssh | 0.81s | **0.4s** |
| tmux | 1.42s | 52.6s |

131x faster on transfer (`scp` versus length-verified base64 chunks through a
PTY), and it removes the whole class of "the pane session dropped" failures.

---

## Layout it creates

```
+---------------------+-------------+
|  editor  (nvim)     |             |
+---------------------+   remote    |
|  cli     (umcares)  |  ssh+Adobe  |
+---------------------+-------------+
```

The right pane must hold a live ssh session — it is the fallback transport.

---

## Commands

| Command | Purpose |
|---|---|
| `inspect [--dir] [--cols N]` | probe media + contact sheets, so an AI can SEE it |
| `recipe example\|validate\|resolve` | author, check and resolve a recipe |
| `render --file R [--from S] [--to S] [--only S…]` | render the recipe into a video |
| `session [--force] [--status] [--reconnect]` | build/inspect the layout, re-open ssh |
| `init [--plan] [--local-only] [--remote-only]` | create the project folder tree |
| `stack check\|up\|down\|status\|logs\|stdio` | MCP backend containers on the remote |
| `doctor [--json] [--quick]` | preflight every dependency |
| `auth [--setup-key]` | ssh credential status, or install a key |
| `config set\|get\|list` | settings and credentials (auto-encrypts secrets) |
| `secrets status\|init\|rotate\|encrypt\|decrypt` | dotenvx-encrypted `.env` (opt-in) |
| `remote <cmd>` | run a command on the Adobe machine |
| `push <local> <remote>` / `pull` | verified file transfer |
| `premiere heal\|status\|report\|import\|build\|export` | control Premiere |
| `media probe\|prepare\|kenburns\|levels` | codec checks, transcode, motion stills |
| `post mix\|subs\|verify-subs` | music bed, subtitles, delivery encode |

Human progress goes to **stderr**, results to **stdout**:

```bash
umcares media probe --dir /path | jq '.needs_transcode'
```

---

## Transport, and why it is the way it is

Two routes, tried in order:

1. **ssh** — clean stdout, real exit codes, `scp` for files. Preferred.
2. **tmux pane** — drives an ssh session a human already logged into. Used when
   no key is available.

The tmux path is hostile territory, and the CLI defends against all of it:

* **Output is base64'd between markers.** A 38-column pane wraps lines and
  injects ANSI; raw output gets silently corrupted. base64 survives because
  every non-base64 character is stripped before decoding.
* **Markers are expanded at runtime** (`echo "${__t}_E"`). If the literal
  marker appeared in the command text, tmux would show it the moment the line
  is *echoed* — before the command ran — and the poller would parse the echo
  as output. This produced intermittent garbage until it was fixed.
* **Uploads are chunked and length-verified after every chunk**, rewinding with
  `truncate` and resending on mismatch. `send-keys` drops input when the shell
  is busy, with no error — a 20 KB JPEG once arrived as 13 KB of corrupt base64.
* **Multi-line scripts are pushed as files and executed**, never typed. A
  heredoc sent as one line makes the remote shell wait forever for its
  terminator.
* **Never types into a busy pane.** Keystrokes land inside whatever is running
  (they ended up inside ffmpeg's interactive prompt more than once).
* **Refuses a pane whose ssh has died.** Such a pane silently becomes a *local*
  shell, and every "remote" command would run on your own machine. The
  transport compares hostnames and bails.

---

## Premiere: why not the MCP server

`AdobePremiereProMCP` advertises 72 tools. Effectively none of them work:

* `premiere.jsx` never parses — ExtendScript is ES3 and the file uses `package`
  as an identifier (`SyntaxError: Illegal use of reserved word 'package'`). It
  defines `mcpDispatch`, which every command routes through.
* The panel loads jsx from `__dirname/host/…`, but CEP sets `__dirname` to the
  extension *root* while the files live under `src/host/`.
* The Go tool schemas were written against `premiere.jsx`, so argument names do
  not match `core.jsx` — `premiere_place_clip` sends `source_path`, core.jsx
  reads `projectItemIndex` (an integer). Unknown args are dropped **silently**,
  so calls "succeed" while placing the wrong clip at the wrong time.

So `umcares` attaches to the CEP panel's DevTools port (9241) and evaluates
ExtendScript directly. `premiere heal` loads `core.jsx` from the correct path
and installs `mcpDispatch` plus helpers. Run it after every Premiere restart.

Helpers are assigned to `$.global` deliberately: a bare `function` inside an
evalScript wrapper is function-scoped and vanishes when the call returns.

---

## Traps encoded in this tool

**VP9 is invisible.** Premiere cannot decode VP9 in MP4. It imports the file
without error, places the *audio*, and drops the video — `overwriteClip` even
returns success. Google Photos re-encodes to VP9 on download. `media probe`
flags it; `media prepare` transcodes to H.264. Always run these first.

**`zoompan` needs `d=1`.** `d=<frames>` emits that many frames *per input
frame*, multiplying duration. A requested 10.3s clip came out at 373s.

**Audio level is normalised, not dB.** Premiere's Volume→Level default reads
`0.17782794` = `10^(-15/20)`, so the parameter maps +15 dB to 1.0:
`value = 10^((dB-15)/20)`. Also, `properties[0]` is *Bypass* — Level must be
found by name.

**`overwriteClip` leaves orphans.** When a later clip covers only part of an
earlier one, the tail survives as a separate clip. `premiere build` sweeps
anything not sitting at a planned start time.

**Check levels per section, never integrated LUFS alone.** One mix measured a
healthy −16 LUFS overall while its opening card sat at −39 dB, inaudible.
`media levels` takes explicit windows.

**Duck past the end of speech, not to the scene boundary.** Lifting music when
a card starts, while the narration is still finishing, buries the last lines.

---

## Secrets

`.env` is read transparently whether plaintext or encrypted.

```bash
umcares secrets status
umcares secrets init             # FIRST RUN: creates .env.keys
umcares config set UMC_SSH_PASSWORD 'hunter2'   # auto-encrypts first
umcares secrets rotate           # new keypair; old key stops working
umcares secrets decrypt          # back to plaintext
```

### Where `.env.keys` comes from

`umcares secrets init` creates it. dotenvx generates the keypair **locally**:
the public key goes into `.env` (so values can be encrypted anywhere) and the
private key into `.env.keys`.

That file exists in exactly one place. There is no server copy and no reset —
**if you lose `.env.keys`, every encrypted value is unrecoverable.** Back it up
in a password manager the moment it is created. It is gitignored by default.

To use the same `.env` on another machine, copy `.env.keys` across by hand, or
export `DOTENV_PRIVATE_KEY` in that environment.

### Rotating

`umcares secrets rotate` decrypts with the current key, mints a **new** keypair,
and re-encrypts. Before touching anything it backs up both `.env` and the old
`.env.keys` (timestamped, gitignored), verifies afterwards that the same set of
keys survived, and restores from backup if re-encryption fails. The old key
stops working immediately, so replace any copy you stored.

Anything matching `KEY|PASSWORD|TOKEN|SECRET` is auto-encrypted on write and
masked in `config list`/`get` unless you pass `--reveal`.

`.env.keys` holds the private key and is gitignored — **it must never be
committed**. Prefer `umcares auth --setup-key` over storing a password at all.

---

## Environment variables

All optional; defaults are in `umcares/config.py`.

Run `umcares config show` to see every effective value and where it came from
(`[env]`, `[.env]`, `[derived]`, or a plain default). Nothing machine-specific
is hardcoded — a different remote, Premiere version or project is config, not a
code edit.

| Variable | Meaning |
|---|---|
| `UMC_REMOTE_HOST` / `UMC_REMOTE_USER` | the Adobe machine |
| `UMC_SSH_ALIAS` | ssh config alias to try first |
| `UMC_SSH_KEY` / `UMC_SSH_PASSWORD` | credentials (key preferred) |
| `UMC_TMUX_SESSION` / `UMC_TMUX_PANE` | session name, or pin a pane like `%5` |
| `UMC_CDP_PORT` | CEP DevTools port (default 9241) |
| `UMC_REMOTE_ROOT` / `UMC_MCP_REPO` | project root, MCP checkout |
| `UMC_PREMIERE_APP` | e.g. `Adobe Premiere Pro 2026` — drives the app + preset paths |
| `UMC_CEP_EXT_ID` | CEP extension id; the panel path derives from it |
| `UMC_PROJECT` | `.prproj` to open — **point this at a scratch project** |
| `UMC_PRESET` | export `.epr`; defaults to AVC-Intra Class100 1080 50p |
| `UMC_NO_SPINNER=1` | disable the spinner |

### Working on a scratch project

`premiere build` **clears the timeline** before laying the new one, so never
aim a recipe at a finished edit:

```bash
UMC_PROJECT=/path/to/scratch.prproj umcares premiere open
UMC_PROJECT=/path/to/scratch.prproj umcares render --file recipe.json
```

`premiere open` is a no-op when that project is already open, and it does the
real work rather than the MCP `premiere_open` tool, which reports
`already_running` and never opens anything.


---

## Folder layout (`umcares init`)

```
assets/
  footage/A      A-roll: speakers, interviews, primary action
  footage/B      B-roll: cutaways, atmosphere
  photos/        source stills
  edit_ready/    H.264 transcodes — the ONLY folder Premiere imports from
  logos/  music/  vo/  cards/
presets/   subtitles/   exports/   project/
```

Created on **both** machines, but the local copy deliberately omits source
media — footage stays on the remote where Premiere can reach it, and out of
git. Each folder gets a `.what-goes-here` note.

Nothing lives under `~/Downloads`, `~/Documents` or `~/Desktop`: macOS TCC
blocks an ssh session from reading those, so media stored there is invisible to
the pipeline and fails much later in a confusing way.

---

## Containers (`umcares stack`)

```bash
umcares stack check                 # docker + compose + port conflicts
umcares stack up --stop-host --build
umcares stack status
umcares stack stdio                 # command for an external MCP client
```

**What cannot be containerized:** Premiere Pro is a licensed macOS GUI app, and
the CEP panel loads inside it. Both stay on the host, so compose must run on
the *remote Mac* and the bridge reaches the panel via
`host.docker.internal:9801`.

Upstream ships only `rust-engine` and `python-intel`. `umcares` writes a
`docker-compose.umcares.yml` override that adds `go-orchestrator` and
`ts-bridge`.

**Port conflicts are checked first.** `scripts/start-all.sh` binds 50052/50053/
50054 on the host; containers publish the same ports and would fail with an
opaque "port is already allocated". `stack up` refuses and points at
`--stop-host`.

**The CLI does not need the stack.** umcares drives Premiere through the CEP
DevTools port precisely because the MCP tool layer is broken. The stack exists
for reproducible media analysis and for serving other MCP clients.


---

## The recipe

`umcares recipe example` writes a worked one. The shape:

```jsonc
{
  "meta":   { "fps": 50, "scene_pad": 0.6, "narration_lead": 0.5 },
  "voice":  { "name": "ms-MY-OsmanNeural", "english_terms": ["scam cyber"],
              "acronyms": ["UM Cares", "PPR"] },
  "cards":  { "impak": { "type": "stats", "tiles": [ {"value": "84%", "label": "…"} ] } },
  "scenes": [
    { "id": "s2_konteks",
      "narration": "Perkembangan teknologi membawa banyak manfaat…",
      "emphasis":  "menjadikan golongan muda sasaran utama",
      "visuals": [
        { "clip": "C0011.mp4" },
        { "kenburns": { "photos": ["DSC01218.JPG", "DSC01220.JPG"] } }
      ] } ],
  "music":  { "file": "…mp3", "start": 25,
              "ducking": [[11,-3], [161,-20], [185,-25], [228,-20], [9999,-3]] }
}
```

**Durations are optional and usually omitted.** `recipe resolve` measures the
rendered narration and the probed clips, then stretches the open visuals so
each scene covers its own narration plus `scene_pad`. A visual with an explicit
`duration` is respected as-is.

`recipe validate` refuses a recipe that references a missing file, an undefined
card, a kenburns with fewer than two photos, non-increasing ducking boundaries
— or **a clip Premiere would import as audio-only**, which is the failure that
looks like success.

## Seeing the media (`umcares inspect`)

Writes into `.umcares/`:

| file | purpose |
|---|---|
| `manifest.json` | every file probed: codec, size, duration, loudness, usable? |
| `sheet_video.jpg`, `sheet_photo.jpg` | tiled thumbnails — what the AI looks at |
| `MEDIA.md` | a briefing: counts, warnings, tile→filename order, file table |

The tile order is emitted explicitly so "tile 6 is the red-shirt speaker" maps
back to a real filename.


---

## Rendering (`umcares render`)

Nine stages, run in order, each idempotent — a re-run redoes only what changed:

| stage | does |
|---|---|
| `voice` | narration -> per-scene WAVs (json2video -> Azure `ms-MY`) |
| `cards` | card specs -> clips (json2video text + ffmpeg logo overlay) |
| `motion` | kenburns specs -> clips (ffmpeg, on the remote) |
| `resolve` | measure everything, place it on the timeline |
| `import` | put the assets into the Premiere project |
| `build` | lay the timeline, mute sync audio, sweep orphans |
| `subs` | SRT timed from the delivered audio |
| `export` | master out of Premiere |
| `deliver` | music bed + soft subtitles -> H.264 |

```bash
umcares render --file recipe.json --only voice   # one stage
umcares render --file recipe.json --to build     # up to a point
umcares render --file recipe.json --force        # regenerate everything
```

`resolve` refuses to continue while anything lacks a measured duration, listing
exactly what is missing. That list is the work queue — it is why the earlier
stages exist, and why a half-generated recipe cannot silently produce black.

### Measure, never assume

Every timing bug this pipeline has had came from arithmetic done by hand:
narration outrunning its scene, music lifted before a sentence ended, 23
seconds of black nobody noticed. So durations are read from the rendered files
and probed clips — never estimated — and `resolve` runs again after generation
so the timeline is built from what exists rather than what was hoped for.

A scene is stretched to cover `narration_lead + narration + scene_pad`. Visuals
with an explicit `duration` are respected; the rest absorb the slack.
