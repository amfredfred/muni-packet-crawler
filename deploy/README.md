# Linux/VPS deployment

Prepared ahead of actual server access being available — **not yet run
against a real machine**. Review the unit files and `install.sh` before
running; the package-manager commands assume Debian/Ubuntu (`apt`). Swap in
`dnf`/`yum`/`pacman` etc. for other distros.

This is the Linux equivalent of the project's `install.ps1` (Windows Task
Scheduler): `muni-webapp` runs persistently via systemd (restarts on crash,
survives reboots); `muni-crawl` and `muni-review` run daily via systemd
timers (2 AM and 3 AM respectively), same as the Windows daily-batch jobs.

## First-time setup

```bash
git clone <this repo's URL> muni-packet-crawler
cd muni-packet-crawler
sudo bash deploy/install.sh install          # everything: webapp + crawl + review
# or just one component:
sudo bash deploy/install.sh install webapp
```

This installs `python3-venv` via `apt`, creates `venv/` and `pip install`s
the project into it, installs Playwright's Chromium + OS dependencies, then
registers and starts the requested systemd units.

`data/muni.db` doesn't exist yet on a fresh clone — the first thing that
connects to it (webapp, crawl, or review) auto-seeds it from the committed
`data/muni_seed.db` (see the main README's "Security notes"), so the
multi-year state-scraping effort doesn't need to be repeated per
deployment. From that point on, this machine's `muni.db` is its own — its
own 2FA registration, its own invoice data, never shared back to git or
any other deployment.

## After installing

- Open `http://<server-ip>:5000` — first visit shows the 2FA setup QR
  code.
- **The Flask dev server is not meant for direct internet exposure.**
  Either firewall port 5000 down to just your own IP, or put a reverse
  proxy with TLS (nginx/Caddy) in front of it before relying on this
  long-term. `install.sh` prints a reminder about this after installing
  the webapp component, but doesn't set up a proxy for you.

## Day-to-day

```bash
systemctl status muni-webapp              # is it running?
journalctl -u muni-webapp -f              # live logs (equivalent of data/webapp.log)
journalctl -u muni-crawl -f               # crawl's last/most-recent run
systemctl start muni-crawl                # trigger a one-off crawl run right now
systemctl list-timers muni-crawl muni-review   # confirm the daily schedule
```

## Updating after a `git pull`

```bash
git pull
sudo bash deploy/install.sh update webapp   # re-copies unit files, restarts the service
```

(`update` re-copies the `.service`/`.timer` files in case they changed and
restarts the webapp; it doesn't touch the venv or re-run `pip install` —
do that yourself first if `pyproject.toml` changed: `venv/bin/pip install -e .`.)

## Uninstalling

```bash
sudo bash deploy/install.sh uninstall        # everything
sudo bash deploy/install.sh uninstall crawl  # just one component
```
