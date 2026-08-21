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
| `recipe example\|validate\|resolve [--markdown]` | author, check and resolve a recipe (`--markdown`: the timeline as a table) |
| `script export\|import\|check` | narration round-trip through markdown, and drift vs the SRT |
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

### One connection, not hundreds

Every command used to be a fresh TCP connect plus key exchange. That is fine
when a human types one; this pipeline probes each asset for existence, pushes
each caption PNG, and polls each render, so the handshakes added up:

```
12 commands, one connection each   3.75s     (~310ms per command)
12 commands, one shared master     1.07s     (~89ms)
6 scp pushes, own connections      2.92s
6 scp pushes, shared master        1.07s
```

`SSHTransport` declares `ControlMaster=auto` with a socket under
`~/.ssh/umcares-cm/`, and `scp` is given the same options — a transfer that
opens its own connection is half the handshakes in a render. `umcares doctor`
prints `ssh (personal, multiplexed)` so the master is visible when it is
working, since that is exactly when nothing else would mention it.

It degrades rather than fails. Multiplexing is an optimisation, so every reason
it cannot run is a reason to carry on without it: no writable `~/.ssh`, or a
socket path that would breach the 104-byte Unix-socket limit (`%C` expands to
40 hex characters, which is budgeted for up front — ssh's own complaint at that
point is about domain sockets, not about ssh). `UMC_SSH_MUX=0` turns it off,
`UMC_SSH_PERSIST` changes how long the master lingers.

A *stale* socket needs nothing: OpenSSH 10.2 unlinks it and opens a fresh
master on its own. A master that is alive but wedged it does not recover from,
so a command failing with `mux_client…` / `read from master failed` drops the
master and runs once more — safe here specifically because that error means the
command never reached the remote.

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

## The script (`umcares script`)

Two things kept going wrong between the recipe and the delivery. `subtitles.srt`
got hand-edited for readability, the recipe kept the old wording, and the next
render put the old wording back — silently, because nothing compared them. And
reviewing the flow meant reading JSON: scene order, wording and captions live in
a file that is otherwise all timings and asset keys.

```bash
umcares recipe resolve --file recipes/v10.json      # windows come from here
umcares script check  --file recipes/v10.json       # SRT vs recipe, per scene
umcares script export --file recipes/v10.json       # -> ./script.md
$EDITOR script.md                                   # rewrite narration
umcares script import --file recipes/v10.json --dry-run
umcares render --file recipes/v10.json --from voice # re-say it, re-cue it
```

`check` classifies rather than just passing or failing, because the differences
call for opposite fixes:

| | meaning | what to do |
|---|---|---|
| `ok` | the SRT is what the recipe says | nothing |
| `edited` | same words, different punctuation or case | adopt it, or ignore it |
| `shifted` | the words exist in the file, in the wrong window | the SRT is from another cut — rebuild it |
| `drift` | the wording differs | decide which side is right, then `script import` |
| `missing` | narration with no cue over its scene window | captions were never built |
| orphan cue | a cue inside no scene's window | it belongs to a cut that no longer exists |

`shifted` exists because the alternative is nine scenes reported as reworded
when the only problem is a stale file, which sends someone rewriting narration
that was already correct.

Two false-positive classes are handled rather than tolerated, because a check
that cries wolf gets muted:

**A cue belongs to one scene.** Counting it in every window it touches made a
caption that overhangs a cut appear as a whole extra sentence in the earlier
scene — reported as drift, when the cue is simply 0.3s on the wrong side. The
biggest overlap decides which scene owns it.

**Narration is TTS input; a caption is display text.** They are *meant* to
differ where a number is involved: the voice says `seratus` because `100` reads
as "satu kosong kosong", and the caption shows `100` because that is what a
number looks like. Declare those pairs and they stop counting as drift:

```jsonc
"subtitles": { "aliases": [["seratus", "100"], ["empat perpuluhan tujuh", "4.7"]] }
```

An alias folds one form onto the other on both sides before comparing, so it
suppresses exactly that substitution and nothing else — `Seramai 100 pelajar`
against `Seramai seratus peserta` is still drift on `peserta` → `pelajar`.

`import` writes back **only** narration and captions — never durations or
visual order. Those are measured from the media (see *Measure, never assume*),
and a markdown table is not a safe place to type numbers the resolver computes.
The visuals table in `script.md` is printed for context and ignored on the way
back. The recipe is backed up to `<recipe>.prev` before it is overwritten.

Scenes are anchored by an HTML comment (`<!-- umcares:scene id=… -->`) rather
than by their heading, so reflowing a paragraph or retitling a heading changes
nothing; an untouched export re-imports as zero changes. A scene the markdown
does not mention keeps whatever the recipe says, so pasting back one section is
safe.

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

Ten stages, run in order, each idempotent — a re-run redoes only what changed:

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
| `deliver` | music bed + subtitles -> H.264 |
| `verify` | compare the delivered file against its own sources |

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

Stretching is capped at each asset's real length. A clip asked to run longer
than it is does not stretch, it ends — and the rest of the slot is black. So a
scene that cannot cover its narration is reported and the build refuses:

```
SHORT s3_pengenalan: 22.0s of visuals for 23.53s of narration — 2.53s would be BLACK
```

## Verifying a delivery (`umcares verify`)

Every other stage reports success from an exit code, which is not the same as
being right. A card patch once went to a dead code path: ffmpeg ran for four
minutes and returned the correct duration, the correct loudness and the correct
byte count, with the wrong picture. Nothing failed.

`verify` compares the delivered file against the things it was made from:

| check | catches |
|---|---|
| picture | a visual that is missing, stale, out of order, or black — each one sampled mid-shot and compared with its source asset |
| captions | a burn that silently did nothing, or captions at the wrong time |
| loudness | per scene, not integrated: an integrated figure once looked fine while the opening card sat at −39 dB |
| duration | drift from the resolved timeline |
| black | gaps nothing else noticed |

```bash
umcares verify --file recipe.json      # no re-render; ~40s for a 4-minute cut
```

It runs as the last render stage too, so `umcares render --file recipe.json`
will not report success on a delivery it cannot vouch for.

Frames are compared as coarse greyscale thumbnails — the question is "is this
the right shot", not "is this bit-identical", which after re-encoding it never
will be. The caption strip is sampled separately and excluded from the picture
comparison only while a cue is actually on screen; excluding it always is how
the stale card slipped through in the first place.

## Staged writes

Nothing overwrites an output in place. Anything that costs minutes, costs API
credits, or holds a human's edits is written to a sibling `.part` file and moved
over the original only once it is complete and non-empty:

| output | why it is staged |
|---|---|
| `master.mxf` | minutes to export, and the input to every delivery |
| delivery `.mp4` | a four-minute encode; a failure used to truncate the good one |
| cards | each one is spent json2video credits, which can run out mid-session |
| narration WAVs | metered too |
| `subtitles.srt` | cheap to regenerate, expensive to re-edit — the previous version is kept as `.srt.prev` |

The reason is not hypothetical. Two title cards were deleted to force a
regeneration, the render API turned out to be out of credits, and the originals
no longer existed — recovering them meant extracting a text layer back out of a
6.8 GB master. A failed render now leaves the previous file exactly as it was.

An empty or suspiciously small result is refused rather than committed, so a
broken encode cannot replace a working master with a 4 KB stub.

## The tmux path, and what testing it found

`capture-pane -J` and the base64 marker protocol are now tested two ways. The
pane is faked at the `tmux` argv boundary — `send-keys` really runs the line
through bash and the result is appended to a buffer `capture-pane` returns — so
`run`, `_capture`, `_parse`, `size`, `push` and `pull` are all real code paths.
Then `tests/test_tmux_live.py` runs the same operations against an actual tmux
session, 40 columns wide so every payload wraps ~200 times:

```bash
python3 -m unittest discover -s tests -t .        # fake pane, ~6s
UMC_TMUX_LIVE=1 python3 -m unittest tests.test_tmux_live -v
```

Both were needed. Three things came out of it:

**A prompt could be spliced into the payload.** `_parse` used to strip every
non-base64 character from the marker region and glue the rest together. But `/`,
letters and digits are all base64, so `irpan@mac ~/DrMadihah %` contributed
`irpanmacDrMadihah` to the output — silently, with nothing to check it against.
Payload extraction now keeps whole lines that are *nothing but* base64, which
excludes furniture and still tolerates a payload split across lines.

**The chunk length check was racing the shell.** `push` typed a 2000-character
chunk, slept a fixed 0.35s, then typed a separate `wc -c`. That second command
was lost whenever the shell was still consuming the first — measured at 40, 80
and 200 columns, 0.35s lost it *every time* and 1.5s never did, and a lost check
meant `size()` waiting out its full 30s timeout before the chunk was resent. So
the append and its length check now travel as one command through the marker
protocol: no fixed pause to tune, and no window to lose the check in. Only a
live pane could show this — the fake executes each line synchronously, so it
never raced.

**A fresh pane has to be woken before it is used.** `tmux new-session` returns
before the shell has finished starting, and a long command typed into a
not-yet-ready zsh is mangled: no markers are printed, the call waits out its
timeout, and the pane is left mid-command so everything after it times out too.
On a 120-column pane, a 2000-character command as the first thing a fresh pane
saw returned 0 bytes after 25s; the same command after one short answered
command took 0.9s. Production is safe by accident of design — `probe()` runs
`echo __UMC_OK__; hostname` and requires an answer before handing the transport
over, which doubles as the wake-up — but anything constructing
`TmuxTransport(pane)` directly skips that, which is how the live test file first
produced eight cascading timeouts and one real bug.

**Polling backs off instead of sleeping a flat 0.4s.** A `capture-pane` costs
~8ms — the subprocess, not the parsing; it measures the same on an empty pane
and after 12KB of output — and a trivial command becomes visible in ~20-25ms.
Waiting a flat 0.4s therefore threw away most of every short command, and this
transport issues one per asset probed. Polling now starts at 10ms and doubles to
the old 0.4s ceiling, so a short command is caught on the second look while a
long export still polls at the rate it always did: **465ms → 218ms per command**
on an idle pane.

**Chunked push is slow for a reason that is not fixable from this side.** After
a 2000-character line the pane's shell cannot accept input for something over
0.5s: with a fresh pane per data point, sending 10 chunks back to back with a
0.0s, 0.25s or 0.5s pause each landed exactly **one** of them. A probe issued
straight after a chunk is swallowed 4 times out of 4. So the per-chunk verify is
not overhead to be optimised away — it is what makes the transfer work at all,
and ~2.5s per 2000 base64 characters (~600 B/s) is the floor for this mechanism.
A 15KB file is 10 chunks and 25s. That is what the tmux path costs; scp over the
ssh transport moves 6 files in 1.07s, which is why ssh is the default and this
is the fallback.

**A bare `exit` takes the ssh session with it.** The command runs inside
`{ ...; }` in the pane's own interactive shell, so `t.run("exit 3")` exits that
shell — which is the session — and the call then waits out its timeout with the
connection already gone. `run_script` documented this; `run` now does too.
Write `( exit 3 )`.

## What this replaced

Before the CLI, the pipeline was a directory of scripts driven by hand. They are
gone; this is where each one went, so a `git log` archaeologist does not have to
guess:

| was | is now | note |
|---|---|---|
| `scripts/j2v_voice.py` | `voice.py` + `voice.*` config | prosody moved from argv to config, so house style lives in one place |
| `scripts/tts_generate.py` | — | Qwen route abandoned: no native Malay voice, and the deployment's `qwen-tts` model does not exist |
| `scripts/j2v_cards.py` | `cards.py` (`stats_scene`, `render`) | |
| `scripts/make_logo_strip.py` | `cards.py` (`content_box`, `build_strip`, `_fit_strip`) | the trim-then-contain fix survived verbatim |
| `scripts/make_logo_cards.py` | `cards.py` (`logo_text_scene`, `split_and_composite`) | |
| `scripts/make_srt.py` | `srt.py` (`speech_spans`, `allocate`, `build`) | same silence detection, but placement comes from the resolved timeline instead of a hardcoded `PLACEMENT` table |
| `scripts/j2v_test.py --check` | `doctor.json2video_auth` | now runs on every `doctor`, instead of when someone remembers |
| root `mcp_*.py`, `raw_ws_test.py`, `remote_cep_test.py`, `patch_panel.py` | — | probes for the MCP tool layer `premiere.py` bypasses on purpose |

Two root scripts survive because nothing here replaces them: `check_narration.py`
whispers the narration WAVs and diffs the transcript against the recipe, and
`compare_subs.py` whispers the delivery and diffs it against the burned cues.
Both are *audio* ground truth; `script check` only compares text to text. Folding
them into `verify` is the obvious next step — there is no `whisper` reference
anywhere in this package today.

## Tests

```bash
python3 -m unittest discover -s tests -t .     # 280 tests, no network, ~6s
```

Every test is a regression test for a bug that actually shipped: the resolver
stretching a clip past its own length into black, captions split at the
midpoint of a pause, `UM Press` spelled out letter by letter, an orphaned
one-word caption line, music lifting before the last sentence ended. The suite
covers the pure logic — resolution, caption timing, SSML, filter strings,
loudness envelopes, config precedence, staged writes — none of which needs a
remote, Premiere, or an API key.

Two suites are about wiring rather than logic, because the worst bug of the
project was a feature passed to a branch that never runs: ffmpeg encoded for
four minutes, reported the right duration and loudness, and produced the wrong
picture. `test_delivery_wiring.py` asserts on the ffmpeg invocation itself —
that patches and captions reach the filter graph, that the encode targets a
staged path, and that the destination changes only through a commit.
