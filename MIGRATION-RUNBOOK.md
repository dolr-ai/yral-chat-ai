# Migration Runbook: Rust → Python Chat Service

This document is the step-by-step guide for migrating from the existing
Rust chat service (`chat.yral.com`) to the new Python chat service
(`chat-ai.rishi.yral.com`).

**Status:** Ready to execute
**Estimated time:** 2-4 hours (including monitoring)
**Risk level:** Medium (DNS switch is reversible)
**Rollback plan:** Switch DNS back to old service IPs

---

## Prerequisites (before starting)

- [ ] New Python service is deployed and healthy at `chat-ai.rishi.yral.com`
- [ ] You have the PostgreSQL connection URL for the OLD chat service
  - Find it in: GitHub Secrets for `dolr-ai/yral-ai-chat` → `PG_DATABASE_URL`
  - Or ask Ravi/Joel for the connection string
- [ ] You have `psql` and `pg_dump` installed locally (`brew install postgresql`)
- [ ] You have Cloudflare access to change DNS for `chat.yral.com`
- [ ] Saikat or another admin is available for DNS changes

---

## Step 1: Get the old database URL (5 minutes)

You need the PostgreSQL connection string for the existing Rust chat service.

**Option A:** Check GitHub Secrets
```bash
# If you have admin access to the repo:
gh secret list -R dolr-ai/yral-ai-chat
# Look for PG_DATABASE_URL — but you can't read the value from CLI.
# You'll need to check the repo settings page on GitHub.
```

**Option B:** Ask Ravi or the team
The connection string looks like:
```
postgresql://username:password@hostname:5432/database_name
```

Once you have it, set it as an environment variable:
```bash
export OLD_DB_URL="postgresql://user:pass@host:5432/dbname"
```

---

## Step 2: Run the migration script (10-30 minutes)

```bash
cd ~/Claude\ Projects/yral-chat-ai
export OLD_DB_URL="postgresql://user:pass@host:5432/dbname"
bash scripts/migrate-from-rust.sh
```

The script will:
1. Dump all data from the old database (influencers, conversations, messages)
2. Load it into our Patroni cluster
3. Backfill the new `sender_id` column
4. Verify row counts match

**Expected output:**
```
  ┌─────────────────┬───────────┬───────────┬─────────┐
  │ Table           │ Old       │ New       │ Match?  │
  ├─────────────────┼───────────┼───────────┼─────────┤
  │ ai_influencers  │      150  │      150  │   ✓     │
  │ conversations   │     5000  │     5000  │   ✓     │
  │ messages        │    50000  │    50000  │   ✓     │
  └─────────────────┴───────────┴───────────┴─────────┘
```

If counts don't match, check the error output. Common issues:
- Duplicate primary keys → safe (ON CONFLICT skips them)
- Column mismatch → the script handles this automatically

---

## Step 3: Verify the migrated data (15 minutes)

### 3a: Check influencers are visible
```bash
curl -s https://chat-ai.rishi.yral.com/api/v1/influencers?limit=5 | python3 -m json.tool
```
You should see the same influencers as the old service:
```bash
curl -s https://chat.yral.com/api/v1/influencers?limit=5 | python3 -m json.tool
```

### 3b: Run the comparison script
```bash
bash scripts/compare-apis.sh
```
All public endpoints should show matching HTTP status codes.

### 3c: Test with a real JWT token (if available)
```bash
bash scripts/compare-apis.sh --with-auth "Bearer YOUR_JWT_HERE"
```
This tests authenticated endpoints like conversation listing.

---

## Step 4: Parallel testing (1-24 hours, optional but recommended)

Before switching DNS, run both services in parallel:
- Old service: `chat.yral.com` (production traffic)
- New service: `chat-ai.rishi.yral.com` (test traffic)

Test manually:
1. Open the mobile app
2. Chat with an AI influencer (this hits the OLD service)
3. Verify the same conversation appears on the NEW service:
   ```bash
   curl -H "Authorization: Bearer TOKEN" \
     https://chat-ai.rishi.yral.com/api/v1/chat/conversations
   ```

---

## Step 5: DNS Cutover (5 minutes)

**This is the big switch.** After this, ALL mobile app traffic goes to the new service.

### 5a: Note the current DNS records
```bash
# Check what chat.yral.com currently points to:
dig chat.yral.com +short
```

### 5b: Update DNS in Cloudflare
1. Log in to Cloudflare dashboard
2. Go to yral.com DNS settings
3. Find the `chat` CNAME or A record
4. Change it to point to our servers:
   - **A record:** `chat.yral.com` → `138.201.137.181` (rishi-1)
   - **A record:** `chat.yral.com` → `136.243.150.84` (rishi-2)
   - (Both records for load balancing)
5. **IMPORTANT:** Also add a Caddy config for `chat.yral.com` on our servers

### 5c: Add Caddy config for chat.yral.com
SSH to rishi-1 and rishi-2 and add a Caddy snippet:
```bash
ssh deploy@138.201.137.181
cat > /home/deploy/caddy/conf.d/yral-chat-production.caddy << 'EOF'
chat.yral.com {
    reverse_proxy yral-chat-ai:8000 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
EOF
# Reload Caddy
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Repeat on rishi-2.

### 5d: Verify the switch
```bash
# This should now hit OUR service:
curl -s https://chat.yral.com/health
# Expected: {"status":"OK","database":"reachable"}

# The old service had a different health response format:
# Old: {"status":"healthy","timestamp":"...","services":{...}}
# New: {"status":"OK","database":"reachable"}
```

### 5e: Test from the mobile app
1. Open the YRAL app
2. Go to Message Inbox
3. Verify existing conversations appear
4. Send a message to an AI influencer
5. Verify you get a response

---

## Step 6: Monitor (1 week)

After the DNS switch, monitor for 1 week:

### Daily checks:
```bash
# Health check
curl -s https://chat.yral.com/health

# Error tracking — check Sentry
# https://apm.yral.com (look for yral-chat-ai project)

# Database health — check Patroni cluster
ssh deploy@138.201.137.181 \
  'docker exec $(docker ps -qf name=chat-ai-db_patroni-rishi-1 | head -1) \
   patronictl -c /etc/patroni.yml list'
```

### What to watch for:
- 500 errors in Sentry → investigate and fix
- Slow responses → check Gemini API latency
- WebSocket disconnections → check container logs
- Mobile app crashes → check with Sarvesh/Shivam

---

## Step 7: Decommission old service (after 1 week)

Only after 1 full week of stable operation:

1. **Keep the old database backup** (just in case):
   ```bash
   pg_dump "$OLD_DB_URL" > yral-ai-chat-final-backup.sql.gz
   ```

2. **Stop the old service** (don't delete yet):
   Ask Ravi or the team to stop the container on the old server.

3. **After 1 more week:** Delete the old deployment if no issues arise.

---

## Rollback Plan

If something goes wrong after DNS switch:

1. **Switch DNS back** in Cloudflare → point `chat.yral.com` back to old server IPs
2. DNS propagation takes 1-5 minutes (Cloudflare is fast)
3. The old service is still running and has all the data
4. Investigate the issue on the new service, fix it, try again

---

## Contacts

| Person | Role | What they can help with |
|--------|------|------------------------|
| Ravi | AI Chat owner | Old database credentials, service architecture |
| Saikat | CTO | DNS changes, admin approvals |
| Sarvesh/Shivam | Mobile devs | App-side issues after migration |
| Rishi | This service | New Python service code and deployment |
