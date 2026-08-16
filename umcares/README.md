# umcares

One CLI for the UM Cares video pipeline: drives Adobe Premiere Pro on a remote
Mac, prepares media, renders cards and voiceover, and produces the delivery.

```bash
export PATH="$PWD/bin:$PATH"

umcares session          # create the 3-pane tmux layout
umcares doctor           # preflight everything
umcares premiere heal    # make Premiere controllable
```

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
| `session [--force] [--status]` | build/inspect the tmux layout |
| `doctor [--json] [--quick]` | preflight every dependency |
| `auth [--setup-key]` | ssh credential status, or install a key |
| `config set\|get\|list` | settings and credentials (auto-encrypts secrets) |
| `secrets status\|encrypt\|decrypt` | dotenvx-encrypted `.env` |
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

| Variable | Meaning |
|---|---|
| `UMC_REMOTE_HOST` / `UMC_REMOTE_USER` | the Adobe machine |
| `UMC_SSH_ALIAS` | ssh config alias to try first |
| `UMC_SSH_KEY` / `UMC_SSH_PASSWORD` | credentials (key preferred) |
| `UMC_TMUX_SESSION` / `UMC_TMUX_PANE` | session name, or pin a pane like `%5` |
| `UMC_CDP_PORT` | CEP DevTools port (default 9241) |
| `UMC_REMOTE_ROOT` | project root on the remote |
| `UMC_NO_SPINNER=1` | disable the spinner |
