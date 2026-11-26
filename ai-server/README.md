# myai: your own private AI server

myai turns one rented server into a private AI that runs day and night,
whether your phone and laptop are on or off. It is made for a **Hostinger
KVM 8** VPS (8 CPU cores, 32 GB memory, no graphics card), and works on any
similar Ubuntu or Debian server.

It installs with one command, and everything runs on your server:

- **A chat page** (Open WebUI) that works in any browser and on your phone.
  It has three modes:
  - **assistant** answers straight away;
  - **assistant-deep** thinks first, for harder questions;
  - **agent** can use tools.
- **An agent that works while you are away.** It can search the web, read
  pages, run Python or shell code in a locked-down sandbox, keep notes and
  files, and run jobs on a schedule ("every Monday at 7, check X and tell
  me"). You can follow its work on a phone-friendly dashboard, and
  optionally talk to it through Telegram.
- **An uncensored ("abliterated") model.** It is an open model with its
  refusals removed. It is a mixture-of-experts design that uses only about
  3 billion of its 35 billion parameters for each piece of text it writes.
  That is what lets it run at a usable speed without a graphics card.
- **Privacy by default.** Strangers who find the server see a blank
  "Private" page. Only devices that have opened your secret entry link get
  through, and then they still need your password.

There are no subscriptions and no API keys. You pay for the server only.

## Be realistic first

- **It is not Claude.** A 35-billion-parameter open model running on CPU is
  good at everyday questions, writing, summaries and simple code. It is
  clearly weaker at hard reasoning, long programs and very long documents.
  Keep your current subscription for a week or two while you try it.
- **It is slower.** Expect roughly **5 to 12 tokens per second** (a token is
  about three quarters of a word), so a 150-word answer takes 15 to 40
  seconds. assistant-deep thinks before it answers, which can add a few
  minutes. That range is an estimate; the installer measures the real speed
  on your server and prints it at the end.
- **The server must not run flat out for hours.** Hostinger lets a VPS use
  about 60% of its CPU all the time, and more only in bursts. myai watches
  this and pauses background jobs before Hostinger slows the server down.
  See [Speed and CPU credits](#speed-and-cpu-credits).
- **Uncensored means no safety net.** The model will answer things a
  normal assistant refuses, and like every model it can be confidently
  wrong. Keep the entry link to yourself; you are responsible for what
  it is used for.

## 1. Rent the server

1. At Hostinger, choose **VPS hosting**, plan **KVM 8**. At the time of
   writing it costs about $26 a month on a 2-year term, and renews at about
   $50 a month; check the current price.
   - The plans with 16 or 32 cores are hourly GPU rentals, not VPS plans.
   - KVM 8 is the largest VPS and cannot be upgraded later.
2. As the operating system, pick plain **Ubuntu 24.04**.
   - Not "Ubuntu 24.04 with Docker", which can occupy the web ports with its
     own proxy.
   - Not a template with a control panel.
3. Set a strong root password, or better, add your SSH key.
4. **Turn on Hostinger's firewall.** A new VPS has none.
   - In hPanel, open your VPS, then **Security → Firewall**.
   - Create a rule set that allows **TCP 22** (SSH), **TCP 80**, **TCP 443**
     and **UDP 443**, and drops the rest.
   - Attach it to the server.
   - The installer also sets up a firewall on the server itself. Hostinger's
     comes first and also covers ports that Docker would open.

A domain name is optional. Without one, the server gets an address like
`srv123456.hstgr.cloud`, or `203-0-113-10.sslip.io` built from its IP
address, with a real HTTPS certificate. To use your own domain, first point
an **A record** at the server's IP address, then add `--domain` below.

## 2. Install

### Option A: one command over SSH (recommended)

Connect with `ssh root@<server IP>`, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/luccadlfn-spec/luccadlfn-spec.github.io/ccr-f9008ee5-63plj0/ai-server/install.sh | bash
```

With your own domain and login e-mail:

```bash
curl -fsSL https://raw.githubusercontent.com/luccadlfn-spec/luccadlfn-spec.github.io/ccr-f9008ee5-63plj0/ai-server/install.sh | bash -s -- --domain chat.example.com --admin-email you@example.com
```

The installer asks a few questions (press Enter for the defaults) and then
works through 11 numbered steps. Along the way it:

- checks the server;
- adds swap, a firewall, fail2ban and automatic security updates;
- installs Docker;
- downloads the model, about 21 GB;
- starts everything and tests it.

Plan on 20 to 45 minutes, mostly for the model download. Keep the window
open.

If the connection drops, just run the same command again. It is safe to
re-run and keeps your settings and data. A detailed log is kept in
`/var/log/myai-install.log`.

Useful options:

| Option | What it does |
|---|---|
| `--domain <host>` | Use your own domain. |
| `--admin-email <e>` | Your chat login name. A real address also becomes the contact for the HTTPS certificate at Let's Encrypt and ZeroSSL. myai itself never sends e-mail. |
| `--model <id>` | Pick a model (see [Models](#models)). |
| `--yes` | Ask no questions. |
| `--dry-run` | Show what would happen and change nothing. |
| `--help` | All options. |

### Option B: Hostinger post-install script

Hostinger can run a script automatically when it installs the operating
system:

1. In hPanel, save the contents of
   [`hostinger/post-install.sh`](hostinger/post-install.sh) as a
   post-install script.
2. Select it when you install or reinstall Ubuntu 24.04.
3. The installation runs by itself with the defaults. Follow it with
   `tail -f /root/myai-install.log`.

Hostinger's **Docker Manager** is not the way to install this. The stack
builds two of its own images, and the Docker Manager's limits (it deletes
data volumes along with a project) do not suit it.

### What you get at the end

The installer prints, and saves in `/root/myai-credentials.txt` (readable by
root only):

1. **Your entry link**, also as a QR code: `https://<your address>/enter/<secret>`.
   Open it once on every phone or computer you want to use. It is your key,
   so do not share it.
2. **Your chat login** (e-mail and password).
3. **The agent dashboard** at `https://<your address>/agent/` and its password.
4. **A Telegram pairing code** (optional).

Show these again at any time with `myai creds`.

## 3. Use it

- **Chat:** open the entry link, log in, and pick **assistant**,
  **assistant-deep** or **agent** at the top of the chat.
  - Add the page to your phone's home screen to use it like an app.
  - In agent mode the tool activity appears in a collapsible "Thought for…"
    block above the answer.
- **Dashboard:** `https://<your address>/agent/` shows the agent's tasks
  with every step it took, its schedules (pause, resume, delete) and its
  notes. You can also start new tasks there.
- **Schedules:** just ask the agent, for example "every weekday at 8:00,
  search for news about X and give me three bullet points". The shortest
  interval is 15 minutes.

### Telegram (optional)

1. In Telegram, write to **@BotFather**, send `/newbot`, and copy the token
   it gives you.
2. On the server, run `myai telegram set` and paste the token.
3. Send your new bot `/pair <code>`, using the code from `myai creds`.

From then on the bot answers only you. Results of scheduled jobs arrive
there too.

## Everyday commands

Run these on the server as root:

| Command | What it does |
|---|---|
| `myai status` | Is everything running? |
| `myai doctor` | Checks address, certificate, memory and disk, with advice |
| `myai link` / `myai creds` | Your entry link (with QR code) / all logins |
| `myai logs [service]` | Recent messages (`llm`, `agent`, `sandbox`, `webui`, `searxng`, `caddy`) |
| `myai start` / `stop` / `restart` | Start, stop or restart everything |
| `myai update` | Get the newest version; settings and data are kept |
| `myai model list` / `myai model use <id>` | See and switch models |
| `myai think on/off/auto` | Whether the agent thinks before answering |
| `myai bench` | Measures the best number of CPU threads (chat pauses 10–15 min) |
| `myai backup` / `myai restore <file>` | Save now / go back to a backup |
| `myai uninstall [--purge]` | Remove myai (`--purge` also deletes data and models) |

A repair check runs every 5 minutes and restarts anything that stopped,
unless you stopped it yourself with `myai stop`.

## Models

All of these models have had their refusals removed. The installer picks the
first one that fits the server's memory. To switch later, run
`myai model use <id>`; it downloads the model if needed.

| id | Size | What it is |
|---|---|---|
| `hauhau` (default) | 21 GB | Qwen3.6 35B (3B active). Fewest refusals. |
| `heretic` | 21 GB | Qwen3.6 35B. Closest to the original model; a few refusals remain. |
| `huihui` | 21 GB | Qwen3.6 35B, classic abliteration. |
| `classic` | 19 GB | Older Qwen3 30B. No thinking mode; the best-known CPU speed. |
| `fast` | 3 GB | Small 4B model for servers with little memory. Much weaker. |

`myai model custom <huggingface repository> <file>` loads any other GGUF
file from Hugging Face.

## Speed and CPU credits

Hostinger's KVM plans run on **CPU credits**:

- A VPS may use about 60% of its cores all the time.
- Unused time is saved up, for about 3 hours at full speed.
- When the savings run out, Hostinger slows the whole server to a fraction
  of its speed until they build up again.

myai's **CPU governor** (on by default on Hostinger) watches for this:

- Chat always comes first.
- Background and scheduled jobs rest between steps, and wait while the
  credits are low.
- The dashboard shows when a job is paused for this reason.

Tips:

- Chats in the same conversation are faster after the first message: the
  model keeps what it has already read.
- assistant-deep and agent with thinking on are much slower. Use plain
  assistant when you need a quick answer.
- Run `myai bench` once after installing. It finds the fastest thread
  setting for your server.

## Backups

- **Automatic:** a backup of your settings, chats, agent data and files is
  made every night at 03:15 server time, in `/opt/myai/backups`. The newest
  7 are kept. The model itself is not backed up; it can be downloaded again.
- **Off the server:** these backups live on the same server, so copy one to
  your own computer now and then:
  ```bash
  scp root@<server IP>:/opt/myai/backups/<file> .
  ```
- **Hostinger:** a second safety net for the whole server, model included.
  - Weekly backups: the last two are kept.
  - One manual snapshot at a time, deleted after 20 days.
  - Both can only restore the entire server, and cannot be downloaded.
- **To go back:** `myai restore /opt/myai/backups/<file>`. It makes a safety
  backup of the current state first, and changes nothing if the archive is
  damaged.

## Security notes

- **The entry link is the key.** If it leaks, make a new one:
  ```bash
  sed -i "s/^GATE_TOKEN=.*/GATE_TOKEN=$(openssl rand -hex 16)/" /opt/myai/.env
  myai restart caddy && myai link
  ```
  Then open the new link on your devices again.
- **Change the chat password** after the first login, in the chat page's
  settings. You can also add other users there; sign-up is closed by default.
- **What leaves the server:**
  - web searches and pages the agent reads;
  - the one-time model download;
  - certificate requests;
  - Telegram messages, if you set it up;
  - updates.

  Your chats and files stay on the server.
- **Admin e-mail:** if you give a real `--admin-email`, it is sent to Let's
  Encrypt and ZeroSSL as the contact for your certificate. With no address
  (or a placeholder), only Let's Encrypt is used.
- **Code sandbox:** code the agent runs has no internet access and cannot
  reach the model server or the rest of the server. It runs as an ordinary
  user in its own read-only container, with limits on memory (1 GB),
  processes and disk use.

## Using Claude Code with the local model (experimental)

[`extras/claude-local.sh`](extras/claude-local.sh) runs the Claude Code
command-line tool on the server against **your local model**. It is not
Claude, and Anthropic does not support it. Because it runs as root, the
model may only read files. See the script's header for details.

## Troubleshooting

- **First step:** run `myai doctor`. It checks everything and says what to do.
- **Browser warns about the certificate right after installing:** wait a
  minute and reload. The certificate is fetched in the first minute.
- **"Port 80/443 is in use":** another web server (Traefik, nginx, Apache)
  is running. The installer names it and says how to stop it.
- **Very slow, or the dashboard says "throttled":** the CPU credits are low.
  Give the server some quiet time. See
  [Speed and CPU credits](#speed-and-cpu-credits).
- **Something is stuck:** run `myai restart`. Your data is kept.

## What has been tested

All of this was built and tested in a 4-core test container, not on a
Hostinger server:

| Suite | Result |
|---|---|
| Agent unit tests | 372 passed |
| Sandbox tests | 105 passed, 1 skipped |
| Installer tests (shellcheck clean) | 269 passed |

**Whole stack:** all six services ran together and were checked end to end:

- the entry link and HTTPS;
- login and all three chat modes;
- every agent tool;
- the dashboard, schedules and one-shot jobs;
- restarts and a full stop and start;
- backup and restore;
- the installer's own self-test.

**Not tested** (blocked from the test machine):

- the official llama.cpp and Open WebUI images;
- Hugging Face downloads, Let's Encrypt and Telegram;
- Hostinger itself and its CPU throttling.

The first real installation is the first time these run together, so
`myai doctor` is your friend.

## What is in this folder

| Path | What it is |
|---|---|
| `install.sh` | The installer, also used by `myai update` |
| `bin/myai`, `bin/myai-lib.sh` | The `myai` command |
| `docker-compose.yml` | The six services: `llm` (llama.cpp), `agent`, `sandbox`, `webui` (Open WebUI), `searxng` (private search) and `caddy` (HTTPS and the private gate) |
| `Caddyfile` | The web front door and the entry-link gate |
| `.env.example` | Every setting, with explanations; the installer writes the real `.env` |
| `agent/` | The agent service (Python), with its tests |
| `sandbox/` | The code sandbox, with its tests |
| `config/` | Model catalogue and search settings |
| `hostinger/post-install.sh` | The Hostinger post-install script |
| `extras/claude-local.sh` | Experimental Claude Code helper |
| `tests/installer/` | Tests for the installer and `myai` |
