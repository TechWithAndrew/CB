# Hosting State Zero Games on Pelican Panel

Use a **Python 3.12 or newer** code egg. A JavaScript/Node.js egg cannot run this Python bot. Egg variables, dependency installation, and startup templates differ between providers; these instructions describe the required behavior rather than assuming a particular egg export. The repository has not been tested on your live Pelican node.

Pelican already runs the egg in a container. The `Dockerfile` and Compose template in this repository are for a separate Docker deployment; they are not needed inside the egg.

## Files and startup

1. Upload the repository contents to the server's application directory, normally `/home/container`. `main.py`, `requirements.lock`, and the `statezero/` directory must be directly inside that directory, rather than inside an extra `CB/` folder. Never upload your development `.venv`, `.git` credentials, or a production token bundled with source code.
2. Select a maintained Python 3.12+ image supported by the egg. Check the actual Python version in the container; the Python version on the panel host does not select the egg's runtime.
3. Configure dependency installation to use **`requirements.lock`** if the egg offers a requirements-file variable. If it only uses `requirements.txt`, ask the provider to adjust the installer or startup template to install the lock file. The required installation command is:

   ```bash
   python -m pip install -r requirements.lock
   ```

   Use the same interpreter/environment for installation and startup. If the image requires a virtual environment, install into `/home/container/.venv` and use `.venv/bin/python` in all commands below. Do not enable automatic package upgrades or automatic Git pulls on every restart.
4. Copy `config.example.json` to `config.json` and enter the real Discord server, role, and channel IDs following README.md. Keep the token out of this file.
5. For eggs with a Python-file variable, set it to **`main.py`**. The final startup command, after any egg dependency-installation step, should be:

   ```bash
   python -u /home/container/main.py
   ```

   Use `/home/container/.venv/bin/python` instead of `python` if you installed into a virtual environment. If your provider uses another application directory, adjust these absolute paths. The `-u` option sends logs to the panel console without Python output buffering.
6. Configure these environment variables through the egg/provider, not as token-bearing command arguments:

   | Variable | Value |
   | --- | --- |
   | `BOT_CONFIG` | `/home/container/config.json` |
   | `BOT_DATA_DIR` | `/home/container/data` |
   | `DISCORD_TOKEN_FILE` | Path to a private mounted token file, if the provider supports one; see below. |
   | `DISCORD_TOKEN` | Actual token, only when using restricted environment storage instead of a file. |
   | `BOT_MESSAGE_CONTENT` | `true` only if complete ticket transcripts are needed and Message Content Intent is enabled in the Developer Portal; otherwise omit or use `false`. |

   Set only the selected token source. `DISCORD_TOKEN_FILE` takes precedence if both are present. A generic Python egg may expose only Git-related variables and no Discord token variable. In that case, use the private-file option below. The bot does not automatically read `.env` files.
7. Before the live startup, run these commands in the container, or temporarily use them as startup commands one at a time:

   ```bash
   python -m pip check
   python /home/container/main.py --config /home/container/config.json --check
   python -m unittest discover -s tests -v
   ```

   A successful `--check` exits normally without connecting to Discord; restore the live startup command afterward. Some eggs restart exited processes, so disable that behavior while running one-time checks. The panel console normally sends input to the running process rather than acting as a general-purpose shell; use the provider's supported installation/terminal workflow.
8. Start once, inspect the console for a successful Discord connection, and use `/bot-status` in your private test server. A panel status of **Running** alone does not prove the Discord connection works. Enable Server Members Intent in the Developer Portal and complete TESTING.md before public rollout.

## Token access and panel permissions

### When the egg has no Discord token variable

The Git token field is for repository authentication, not Discord. With neither `DISCORD_TOKEN_FILE` nor a nonempty `DISCORD_TOKEN` set, the bot reads **`secrets/discord_token`** relative to its working directory. For the usual egg working directory `/home/container`:

1. Stop the bot and create a `secrets` folder in the panel's Files area.
2. Inside it, create a file named `discord_token` with only the bot token as its contents. Create it directly on the host; do not bundle it in a source upload or share its contents.
3. Set the file's Unix permissions to **`600`** and, where possible, the folder's to `700`. Use an SFTP client with permission controls, a supported panel permission editor, or ask the provider to do this. A node administrator can run `chmod 600 /home/container/secrets/discord_token` as the runtime user or with appropriate privileges. The bot refuses group/world-readable token files; do not bypass this check.
4. Keep `main.py` as the startup script. No extra egg variable is needed. Make sure startup uses `/home/container` as its working directory, as it does for the default relative configuration and data paths.

`secrets/` is excluded from Git and Docker build contexts. This file is still accessible to privileged panel/node users and may be included in panel backups. Restrict file/SFTP/backup permissions and protect backups. The following external-mount option provides better separation when your provider supports it.

Prefer a provider-managed **read-only secret mount outside `/home/container`** when available. The file must be readable by the egg's actual runtime user and have mode `0600`; do not assume the Docker template's UID 10001 applies to your Pelican egg. Your node administrator must arrange the mount and ownership. Do not create a path such as `/run/secrets/discord_token` in settings unless a readable file is actually mounted there.

If the provider supports only environment variables, restrict who can view/edit startup variables and who can access the container, console, files/SFTP, or backups. Check the egg's visibility/editability settings and effective subuser permissions with the provider. Hiding a field in the UI does not encrypt it or keep it secret from panel/node administrators. A person who can replace executable code can also read the token at runtime.

Never put the token in an egg's exported JSON/default values, startup command, repository URL, screenshot, or config file. Do not assume variables are encrypted at rest without verifying the provider's storage and backup controls. If you must store a private token file inside the server volume, protect it with mode `0600` and account for its inclusion in panel backups and file access; this is weaker isolation than an external managed mount. Rotate the token immediately after an exposure or loss of trusted host access.

Use separate development and production bot tokens. Require MFA on the panel/hosting/GitHub accounts, grant subusers only necessary permissions, and keep the panel, Wings, container image, and host maintained. Discord staff roles do not require panel access.

## Storage, resources, and recovery

- Keep `config.json` and `/home/container/data` on the server's persistent volume. Confirm they survive ordinary restart and node reboot. Run exactly **one instance** using this bot token and SQLite database. The database is `data/statezero.db`; its WAL/SHM sidecars may also be present.
- Take consistent backups with the bot stopped, or with SQLite's backup API. Copying only a live `.db` file can miss WAL transactions. Encrypt/restrict backups, test restoring them to a stopped staging instance, and keep mounted secrets separate when possible. Backups do not recreate deleted Discord channels.
- Stop before uploading a release. Update application files only; preserve `config.json`, `data/`, and the selected secret source. Avoid destructive reinstall, sync, or `git clean` procedures unless you have verified backup/restore and understand what the provider deletes.
- Ask the provider to use a graceful interrupt (`SIGINT`) for stopping the Python process where the egg supports it, and allow time for shutdown. Test both stop/start and forced interruption in staging. Restarting the container must reconnect and preserve existing panels, open tickets, and the verification lock.
- Enable supported restart-on-failure and node-reboot recovery. Verify the behavior on your provider; a crash loop should produce an alert rather than repeated unnoticed retries.
- Start with approximately 2 vCPU and 2 GB RAM **available to the bot's container**, then measure actual traffic. These are planning figures, not demonstrated capacity requirements. A shared node's total resources are not the same as your egg's CPU/RAM limits. Keep swap/disk limits from hiding memory or storage pressure.
- Use panel resource graphs and console logs, plus external uptime/error alerts. The bot currently has no metrics dashboard; confirm an operator receives alerts and can act. Complete the busy-period pilot, permissions, recovery, and traffic checks in TESTING.md.
- The bot opens no inbound listening port. If the panel requires a port allocation, it is unused by this application. Allow outbound Discord HTTPS and secure gateway WebSockets, plus CDN access for transcripts as needed. Do not expose a database or Docker socket to make the bot work.

The existing ticket crash-recovery limitation documented in TESTING.md still applies on Pelican. A different host does not establish public-server readiness or eliminate application security/recovery checks.
