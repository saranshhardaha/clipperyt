# Run the dashboard on boot (Linux)

```bash
sudo cp -r ~/clipperyt /opt/clipperyt            # or clone/copy it there
cd /opt/clipperyt && ./setup_linux.sh
sudo cp deploy/clipperyt.service /etc/systemd/system/
sudo nano /etc/systemd/system/clipperyt.service  # set User= and CLIPPER_TOKEN=
sudo systemctl daemon-reload && sudo systemctl enable --now clipperyt
journalctl -u clipperyt -f                       # logs
```

Open `http://<server-ip>:8787/?token=<your token>` once. A cookie remembers it after that.

To start watching automatically whenever the dashboard boots:

```bash
curl -X POST "http://localhost:8787/api/start?token=<token>"
```

or add `ExecStartPost=/bin/sh -c 'sleep 3; curl -s -X POST "http://127.0.0.1:8787/api/start?token=$CLIPPER_TOKEN"'` to the unit.
