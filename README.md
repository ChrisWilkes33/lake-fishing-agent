# 🎣 Lake Fishing Agent (Fish Warden)

An AI agent that monitors fishing reports for Lake Buchanan and sends a nightly email digest with summaries written in angler shorthand. Also serves reports as JSON at `wilkeslandia.com/fishing-agent/reports.json` for the future Android app.

---

## One-time server setup (do this once on your Nanode)

### 1. Create the fishing-agent user

Running agents as root is a security risk. We use a dedicated low-privilege user instead.

```bash
# SSH into your Nanode as root first
adduser fishingagent --disabled-password --gecos ""
```

### 2. Create the web output directory

The agent writes `reports.json` here so nginx can serve it.

```bash
mkdir -p /var/www/fishing-agent
chown fishingagent:fishingagent /var/www/fishing-agent
```

### 3. Generate an SSH key for GitHub Actions

This key lets GitHub Actions SSH in as fishing-agent to deploy code.

```bash
# Run this as root on your Nanode
su - fishingagent
ssh-keygen -t ed25519 -f ~/.ssh/github_actions -N "" -C "github-actions"
cat ~/.ssh/github_actions.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
cat ~/.ssh/github_actions   # copy this — you'll need it in step 6
exit  # back to root
```

### 4. Clone the repo as fishing-agent

```bash
su - fishingagent
git clone https://github.com/ChrisWilkes33/lake-fishing-agent.git
cd lake-fishing-agent
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your values
mkdir -p logs
exit  # back to root
```

### 5. Set up cron as fishing-agent (midnight daily)

```bash
crontab -u fishingagent -e
```

Add this line:
```
0 0 * * * cd /opt/fishing-agent && python monitor.py >> logs/monitor.log 2>&1
```

To switch to Thursdays only after evaluating cost:
```
0 0 * * 4 cd /opt/fishing-agent && python monitor.py >> logs/monitor.log 2>&1
```

### 6. Add GitHub Actions secrets

Go to: github.com/ChrisWilkes33/lake-fishing-agent → Settings → Secrets and variables → Actions

Add two secrets:
- `NANODE_HOST` — your Nanode IP or `wilkeslandia.com`
- `NANODE_SSH_KEY` — the private key you copied in step 3 (the whole thing including the `-----BEGIN` and `-----END` lines)

### 7. Update nginx to serve fishing-agent reports

Add this block to your nginx config in the wilkeslandia repo (`nginx/wilkeslandia.conf`), inside the main server block:

```nginx
# Fish Warden — fishing report JSON for Android app
location /fishing-agent/ {
    alias /var/www/fishing-agent/;
    add_header Access-Control-Allow-Origin "*";
    add_header Cache-Control "no-cache";
}
```

Then deploy the wilkeslandia repo to pick up the nginx change, or manually:
```bash
nginx -t && systemctl reload nginx
```

---

## Running the agent

### First run — build your source list

```bash
su - fishingagent
cd lake-fishing-agent
python discoverer.py
```

To discover sources for a different lake:
```bash
python discoverer.py --lake "Lake Travis Texas"
```

### Test the monitor (before cron takes over)

```bash
python monitor.py --test
```

This runs the full monitor, sends a real email, writes real state, and prints a detailed cost breakdown so you can decide on daily vs weekly schedule.

### Normal run (what cron does)

```bash
python monitor.py
```

---

## After setup — ongoing workflow

Once deployed, you never touch the Nanode again for code changes:

1. Edit code locally or here in Claude
2. Push to GitHub
3. GitHub Actions SSHs in and pulls automatically
4. Done

---

## File reference

| File | Location on Nanode | Purpose |
|------|--------------------|---------|
| `discoverer.py` | `/opt/fishing-agent/` | Agent 1 — run manually |
| `monitor.py` | `/opt/fishing-agent/` | Agent 2 — run by cron |
| `.env` | `/opt/fishing-agent/` | Your secrets — never in git |
| `discovered_sources.json` | `/opt/fishing-agent/` | Source list from discoverer |
| `sent_reports.json` | `/opt/fishing-agent/` | Dedup tracker — never delete |
| `reports.json` | `/var/www/fishing-agent/` | Served publicly for Android app |
| `logs/monitor.log` | `/opt/fishing-agent/` | Cron run history |

---

## Cost estimates

Both agents use Claude Haiku (cheapest model).

| Schedule | Estimated cost |
|----------|---------------|
| Daily | ~$2-5/month |
| Weekly (Thursdays) | ~$0.25-1/month |

Run `python monitor.py --test` to see the exact cost for your source list.

---

## Troubleshooting

**GitHub Actions deploy fails**
- Check the Actions tab in GitHub for the error
- Make sure `NANODE_HOST` and `NANODE_SSH_KEY` secrets are set correctly
- Verify the fishing-agent user exists: `id fishingagent` on the Nanode

**Email not sending**
- Use a Gmail App Password, not your regular password
- Requires 2-Step Verification on the Gmail account
- Generate at: myaccount.google.com/apppasswords

**Agent finds no reports**
- DuckDuckGo occasionally blocks scrapers — wait an hour and retry
- Re-run `discoverer.py` to refresh the source list

**reports.json not accessible at wilkeslandia.com/fishing-agent/**
- Check the nginx block was added and nginx was reloaded
- Verify `/var/www/fishing-agent/` is owned by fishing-agent: `ls -la /var/www/fishing-agent/`
