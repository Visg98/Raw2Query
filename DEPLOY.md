# Deploying raw2query

The frontend is a static SPA and goes on Netlify. The backend cannot go anywhere
serverless, so it runs as Docker Compose on a single small VM.

## Why a VM and not a PaaS

Three properties of the app rule out the usual free serverless hosts:

- **The API needs torch at import time.** `app/api.py` pulls in the query router, which reaches
  `app/pipeline/embeddings.py` through `app/query/rag.py`. That puts the API at ~1.0–1.3 GB
  resident, so the common 512 MB free tiers cannot run it.
- **The worker is a separate always-on process.** `app/worker.py` is a blocking poll loop claiming
  jobs with `SELECT ... FOR UPDATE SKIP LOCKED`. It is not request-driven, so anything that
  scales to zero cannot host it.
- **The API and worker share a filesystem.** The API writes uploads to `UPLOAD_DIR`; the worker
  reads those exact paths back (`partition(filename=...)`), and previews and original-file
  downloads read them again later. This needs one shared, persistent volume.

Postgres runs as a container rather than a managed service for one specific reason:
`migrations/versions/0001_initial.py` needs `CREATE EXTENSION` and `CREATE ROLE ... LOGIN`, and it
reads `POSTGRES_DB` from the environment for its `GRANT CONNECT`. The pgvector image's
`POSTGRES_USER` is a superuser, so the migration just runs. Most managed free Postgres tiers would
fight at least one of those steps.

## Architecture

```
Netlify                           VM (2 vCPU / 12 GB)
┌──────────────────┐              ┌────────────────────────────────────────────┐
│ React SPA        │   HTTPS      │ caddy  :80/:443   TLS + demo-token gate    │
│ frontend/dist    │─────────────▶│   └─▶ api    127.0.0.1:8000                │
└──────────────────┘              │       worker ×1                            │
                                  │       db     (not published)               │
                                  │  volumes: uploads, hf_cache, db_data,      │
                                  │           caddy_data                       │
                                  └────────────────────────────────────────────┘
```

TLS is required, not cosmetic: the Netlify page is HTTPS, and a browser blocks an HTTPS page from
calling a plain-HTTP API as mixed content.

## 1. The VM

Any provider works. These instructions assume Oracle Cloud's Always Free tier (Ampere A1, 2 OCPU /
12 GB, ARM) because it is always-on and does not expire.

1. **Launch the instance.** Ubuntu 24.04 (aarch64), shape `VM.Standard.A1.Flex`, 2 OCPU / 12 GB,
   **100 GB boot volume** — the ~47 GB default gets tight once the images are built. Save the SSH
   private key at creation time; it cannot be downloaded again.

   If you get *"Out of host capacity"*, that is the single most common failure here — Always Free
   A1 capacity is heavily contested. Try each availability domain in your region. Upgrading the
   account to Pay-As-You-Go improves availability materially and leaves Always Free resources
   free.

2. **Reserve the public IP.** Networking → IP addresses → change the ephemeral IP to reserved.
   Ephemeral IPs change on stop/start, which would break both DNS and certificate renewal.

3. **Open ingress** in the subnet's security list: TCP 80 and 443 from `0.0.0.0/0`.
   **Not** 5432, and **not** 8000.

4. **Open the host firewall too.** Oracle's images ship an `iptables` INPUT chain ending in
   `REJECT`, so the security list alone is not enough — this is why a correctly configured instance
   can still be silently unreachable. Insert the rules *above* the REJECT:

   ```bash
   sudo iptables -L INPUT --line-numbers          # find the REJECT line number, e.g. 6
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

   Do **not** run `ufw enable` on an Oracle image — it flushes these rules and can lock you out of
   SSH.

5. **Install Docker** and add swap. Oracle's image ships no swap, and a pip resolve can be
   OOM-killed without it:

   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker ubuntu     # then log out and back in

   sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

## 2. DNS

Register a free subdomain at [duckdns.org](https://www.duckdns.org) pointing at the reserved IP.
Any domain works; DuckDNS just avoids buying one.

## 3. Configuration

```bash
git clone <your-repo-url> && cd raw2query
cp .env.example .env
```

Edit `.env` and set:

| Variable | Notes |
|---|---|
| `GROQ_API_KEY` | The LLM provider key. |
| `QUERY_RUNNER_DB_PASSWORD` | **Set this before the first migration.** Migration 0001 bakes it into the `raw2query_query_runner` role it creates, and it must match the password inside `READONLY_DATABASE_URL`. Changing it later means re-running the migration or altering the role by hand. |
| `DOMAIN` | e.g. `yourname.duckdns.org`. Used by Caddy for certificate issuance. |
| `DEMO_TOKEN` | A long random string. Must match the `VITE_DEMO_TOKEN` you set in Netlify. |

`.env` is git-ignored and must stay that way.

## 4. Build and migrate

```bash
export COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$COMPOSE build          # 10-40 min on 2 cores; builds one image for api + worker
```

Build **on the VM**, not locally — if your workstation is x86 and the VM is ARM, a local build
produces the wrong architecture.

Then bring the database up and migrate it *before* starting the API. This ordering is required, not
tidiness: `depends_on` only waits for the database to be *healthy*, and the API's
`seed_uncategorized_topic` startup hook queries the `topics` table with no error handling, so on a
fresh database the API crash-loops until the schema exists.

```bash
$COMPOSE up -d db
$COMPOSE run --rm api alembic upgrade head
$COMPOSE up -d
```

Caddy requests a Let's Encrypt certificate on the first HTTPS request. Verify:

```bash
curl https://yourname.duckdns.org/health     # {"status":"ok"}
```

Note `/health` does no database check, so a successful response does not mean the app works. Hit a
real endpoint too — `/topics` should return JSON (it needs the token header, see below).

## 5. Netlify

1. Connect the repository. `netlify.toml` already supplies the base directory, build command,
   publish directory and the SPA catch-all redirect, so no dashboard configuration is needed.
2. Set two build environment variables:
   - `VITE_API_BASE_URL` = `https://yourname.duckdns.org`
   - `VITE_DEMO_TOKEN` = the same value as `DEMO_TOKEN` in the VM's `.env`

   Vite inlines both at **build** time, so they must exist before the first build, and changing
   either one requires a redeploy.
3. Deploy, then walk through upload → review → confirm → query.

## Access control

The API has no authentication. Caddy checks a shared `X-Demo-Token` header instead, with two
deliberate exemptions (see `docker/Caddyfile`):

- **`OPTIONS`** — a custom header triggers a CORS preflight, and the preflight does not carry the
  header. Gating it would break every request.
- **`GET /documents/*` and `GET /jobs/*`** — `fileUrl()` feeds `<img>`/`<a>` attributes directly and
  the progress stream uses `EventSource`, neither of which can set headers. These are read-only,
  keyed by unguessable UUIDs, and cost no LLM calls.

Uploads, `/query` and `/chats` stay gated, which is where the cost and abuse potential are.

Be clear about what this is: the token is inlined into a public JS bundle, so it deters crawlers
and drive-by scanners, not a determined person. The provider's own rate limit is the real backstop.
If you move to a paid LLM provider, set a hard spend cap on the key — that, not the token, bounds a
money risk.

## Operating notes

**Logs and status**

```bash
$COMPOSE ps
$COMPOSE logs -f api
$COMPOSE logs -f worker
```

**A job stuck in `extracting`.** There is no stale-job reaper: `app/worker.py` sets `locked_at` /
`locked_by`, but nothing requeues a job whose worker died mid-extraction. If the worker is OOM-killed
or restarted during a large PDF, the job stays claimed forever and the UI polls indefinitely.
Recover manually:

```bash
$COMPOSE exec db psql -U raw2query -c \
  "UPDATE jobs SET status='pending', locked_at=NULL, locked_by=NULL WHERE status='extracting';"
$COMPOSE restart worker
```

**Large uploads.** `app/routers/documents.py` reads each upload fully into memory, once per file in
the batch, so a big multi-file batch is a real memory spike. Caddy caps bodies at 50 MB to bound it.

**Why one worker.** With 2 vCPU, torch and onnxruntime each try to use every core, so a second OCR
worker reduces throughput while doubling peak memory. Scale the VM before scaling the worker.

**Data durability.** Structured records, chunks and embeddings live in Postgres (`db_data`).
Uploaded originals live in the `uploads` volume; if it is lost, extracted data survives but previews
and original-file downloads return "no longer on disk", and re-extraction fails.

## Verification checklist

```bash
# torch is the CPU build - no CUDA packages pulled in
$COMPOSE run --rm api python -c "import torch; print(torch.cuda.is_available())"   # False
$COMPOSE run --rm api sh -c "pip list | grep -i nvidia"                            # empty

# the privileged part of migration 0001 ran
$COMPOSE exec db psql -U raw2query -c "\dt"     # tables exist
$COMPOSE exec db psql -U raw2query -c "\du"     # raw2query_query_runner role exists

# Postgres is NOT reachable from outside (run from your laptop)
nc -vz <vm-ip> 5432                              # must fail
```

Then end-to-end from the Netlify URL, which is the only check that exercises CORS, TLS, the token
gate and the shared volume together: upload a PDF, watch progress advance, confirm a record in
review, then ask a question and confirm you get both a SQL-backed and a RAG-backed answer. Finally
open a document preview and download the original — if both work, the API and worker really are
sharing the uploads volume.
