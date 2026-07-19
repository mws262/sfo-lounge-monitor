# Deploying on the Hetzner VPS

A oneshot systemd service driven by a timer (every 3 min). The pollers are pure
stdlib HTTP, so nothing to build.

```bash
# 1. Put the code on the box
sudo mkdir -p /opt/sfo-lounge-monitor /var/lib/sfo
sudo rsync -a ./ /opt/sfo-lounge-monitor/          # from your checkout
sudo useradd -r -s /usr/sbin/nologin sfo || true
sudo chown -R sfo /var/lib/sfo

# 2. (Optional) drive-time signal — env vars win over config.toml
sudo tee /opt/sfo-lounge-monitor/sfo.env >/dev/null <<'EOF'
# SFO_GOOGLE_ROUTES_API_KEY=...
# SFO_DRIVE_ORIGIN=37.77,-122.41
EOF
sudo chmod 600 /opt/sfo-lounge-monitor/sfo.env

# 3. Install the units
sudo cp deploy/sfo-monitor.service deploy/sfo-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sfo-monitor.timer

# 4. Watch it
systemctl list-timers sfo-monitor.timer
journalctl -u sfo-monitor.service -f
sqlite3 /var/lib/sfo/club_sfo.db 'select * from airport order by ts desc limit 5;'
```

**Two cadences, one timer.** The timer fires every 3 min, so the **lounge**
(and the small security/METAR/FAA signals) refresh every 3 min. The heavy
**flight board** is throttled separately by its 20-min disk cache
(`--board-ttl 1200`, persisted via `--cache-dir`), so it's pulled only ~3×/hour
regardless of the timer. That keeps the lounge fresh while board bandwidth stays
at ~36 MB/day. 3 min is above the ≥60s courtesy floor — don't go below 60s.

**Notifications (next step):** wrap the ExecStart in a small script that reads
the last two rows and pushes via ntfy/Pushover on a threshold crossing (lounge
FULL→OPEN, or composite crossing a "leave now" line). Not built yet — see the
roadmap in the top-level README.
