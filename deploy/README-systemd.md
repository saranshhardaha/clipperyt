# Run the dashboard automatically

```bash
./deploy/install_service.sh              # Linux: systemd on boot · macOS: LaunchAgent at login
./deploy/install_service.sh --uninstall
```

The script:
- generates a dashboard token (kept in `.dashboard_token`) unless you pass `CLIPPER_TOKEN=…`
- Linux: writes `/etc/systemd/system/clipperyt.service` from `deploy/clipperyt.service` with your user and this folder, then enables it (`journalctl -u clipperyt -f` for logs)
- macOS: writes `~/Library/LaunchAgents/com.clipperyt.dashboard.plist` (log: `logs/dashboard.log`)
- prints the link to open once per device: `http://<ip>:8787/?token=…`

Both setups also take `--service` to do this at the end: `./setup_linux.sh --service`, `./setup.sh --service`.

To start recording automatically after a reboot, tick **autostart** on a job in the dashboard. Jobs run as their own processes, so restarting the dashboard (or the service) never cuts a recording.

Moving to `/opt/clipperyt` on a server: copy or clone the folder there, run `./setup_linux.sh --service` from inside it.
