# Deploying the review app on Azure

Target setup: the review app runs as its own Container App (`btr-review`) in the
same Container Apps environment as the climate-policy-miner (resource group
`ndc-tracker`, region Germany West Central), and stores its data in an Azure
Database for PostgreSQL flexible server. The database is meant to be shared
later with the Tracker's own tables (schemas `ndc`, `ref`); the BTR tables live
in schema `btr`.

Most steps use the Azure portal. Three things cannot be done there:

| Step | Where | Why |
|---|---|---|
| Copy the data, create the app's database login | PowerShell on your machine, repository root | Python commands of this repository |
| Build the container image | Cloud Shell in the portal (`>_` icon in the top bar) | One `az acr build` command, builds straight from GitHub |
| Sign-in | GIZ IT | Needs an app registration in Entra ID |

Portal labels change from time to time; if a label below does not match
exactly, look for the nearest equivalent.

## Values to keep

Store these in a password manager as you create them:

| Value | Example | Used in |
|---|---|---|
| Server name | `ndc-tracker-db` | all connection URLs |
| Admin password | 24+ letters and digits | step 3, schema updates |
| App password | 24+ letters and digits | step 3, step 7 |
| Flask secret | 32+ random characters | step 7 |

Use letters and digits only in the database passwords: special characters
would have to be escaped inside the connection URLs.

The two connection URLs follow this pattern:

```
postgresql://tracker_admin:<admin-password>@<server>.postgres.database.azure.com:5432/tracker?sslmode=require
postgresql://btr_app:<app-password>@<server>.postgres.database.azure.com:5432/tracker?sslmode=require
```

The first (admin) is for you and the pipeline; the second (app) is for the
review app only.

## 1. Create the database server

*Create a resource* → **Azure Database for PostgreSQL – Flexible Server** → Create.

**Basics**
- Resource group: `ndc-tracker`
- Server name: your choice (must be unique across Azure)
- Region: Germany West Central
- PostgreSQL version: 16
- Workload type: Development → *Configure server*: Burstable, **Standard_B1ms**,
  storage 32 GiB, high availability disabled. Check the monthly estimate shown.
- Authentication: *PostgreSQL authentication only*; admin login
  `tracker_admin` and the admin password.

**Networking**
- Connectivity method: *Public access (allowed IP addresses)*
- Leave **unticked**: *Allow public access from any Azure service within Azure
  to this server*. It admits resources of every Azure customer, not only yours.
- Click *+ Add current client IP address*.

Review + create. Provisioning takes a few minutes. If GIZ's Azure policies
forbid public network access, creation fails at this point; then private
networking is needed and IT has to be involved.

Then open the server → **Databases** → *Add* → name `tracker` → Save.

Your public IP changes with network and VPN. When it does, add the new one
under the server's **Networking** → *Firewall rules*.

## 2. Prepare your machine

In the repository root (once):

```powershell
git pull
pip install -r requirements.txt
```

Stop the local review app if it is running: the copy reads your working copy
`data/btr_analysis.local.db`. Bring in everyone's latest decisions first, so
the online database starts complete:

```powershell
python pipeline.py import-decisions
```

Resolve any conflicts it reports before copying (see `SHARING_DECISIONS.md`).

## 3. Copy the data and create the app login

```powershell
$ADMIN_URL = "postgresql://tracker_admin:<admin-password>@<server>.postgres.database.azure.com:5432/tracker?sslmode=require"

python pipeline.py --db $ADMIN_URL copy-db --from data/btr_analysis.local.db
```

The command creates the `btr` schema, copies every table with its ids and
verifies each table row by row. It must end with
`Copy verified: all tables identical.` It refuses to write into a database
that already holds data unless you pass `--replace`.

Then the login the app will use. It can read and write rows but cannot change
or drop tables:

```powershell
$env:BTR_APP_DB_PASSWORD = "<app-password>"
python pipeline.py --db $ADMIN_URL create-app-role
Remove-Item Env:BTR_APP_DB_PASSWORD
```

If the copy times out, the network you are on probably blocks outgoing
connections on port 5432 (common in office networks). Try another network and
add its IP to the server's firewall rules.

## 4. Build the container image

Open **Cloud Shell** in the portal (`>_` in the top bar, Bash or PowerShell)
and run:

```
az acr build -r transportpolicyminer -t btr-review:1 https://github.com/Changing-Transport/btr-analysis.git#main
```

This builds the image from the `main` branch on GitHub and stores it in the
miner's registry as `btr-review:1`. The image contains only `btr_pipeline/`,
`review_app/` and `wsgi.py` (see `.dockerignore`); no database file goes into
it. For later versions use the next tag number (`btr-review:2`, ...).

## 5. Create the Container App

*Create a resource* → **Container App** → Create.

**Basics**
- Resource group: `ndc-tracker`
- Container app name: `btr-review`
- Region: Germany West Central
- Container Apps environment: select the existing environment of
  `transport-policy-miner` (do not create a new one)

**Container**
- Untick *Use quickstart image*
- Image source: Azure Container Registry; registry `transportpolicyminer`,
  image `btr-review`, tag `1`
- CPU and memory: 0.5 CPU cores, 1 Gi memory
- Environment variables: add one, `BTR_EASY_AUTH` = `0`

**Ingress**
- Ingress: enabled; traffic: *Accepting traffic from anywhere*
- Ingress type: HTTP; target port: **8000**

Review + create. The first revision will not start yet (the database settings
come in step 7); that is expected.

## 6. Restrict access to your IP

Until sign-in exists (step 10), anyone who knows the URL could record
decisions, so only your IP may open the app:

`btr-review` → **Settings → Ingress** → *IP security restrictions mode*: allow
only the IPs listed → add a rule with your IP as `x.x.x.x/32` → Save.

## 7. Secrets, settings and scale

**Settings → Secrets** → *Add*, three times:

| Secret name | Value |
|---|---|
| `db-url` | the **app** connection URL (`postgresql://btr_app:...`) |
| `flask-secret` | the Flask secret |
| `aoai-key` | the Azure OpenAI key the miner uses |

**Application → Containers** → *Edit and deploy* → select the container →
**Environment variables**, add:

| Name | Source | Value |
|---|---|---|
| `BTR_DATABASE_URL` | Reference a secret | `db-url` |
| `FLASK_SECRET_KEY` | Reference a secret | `flask-secret` |
| `AZURE_OPENAI_API_KEY` | Reference a secret | `aoai-key` |
| `AZURE_OPENAI_ENDPOINT` | Manual entry | same as the miner |
| `AZURE_OPENAI_API_VERSION` | Manual entry | same as the miner |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME` | Manual entry | same as the miner |
| `BTR_EASY_AUTH` | Manual entry | `0` (already there) |

The miner's values are under `transport-policy-miner` → *Containers* →
*Environment variables*. The Azure OpenAI settings let the app generate NDC
candidates the moment a measure is confirmed transport-relevant; without them
that step falls back to `pipeline.py match`.

In the same *Edit and deploy* view, **Scale**: min replicas `0`, max replicas
`1`. With 0 the app sleeps when unused; the first request after a pause takes a
few seconds.

*Create* to deploy the new revision.

## 8. Let the app reach the database

`btr-review` → **Settings → Properties** (or *Overview*, JSON view) → copy the
**outbound IP addresses**.

Database server → **Networking** → *Firewall rules* → add one rule per address
(start and end IP the same; name e.g. `btr-review-out-1`) → Save.

These addresses rarely change, but they can. If the app later reports
database timeouts, compare them again.

## 9. Check

Open `https://<app-url>/healthz` (the URL is on the app's *Overview*). It
should show `{"status": "ok"}`. Then open the app itself, enter your initials,
and open one measure in *Browse*.

If it fails: `btr-review` → **Monitoring → Log stream** shows the error.
Common causes:

| Message in the log | Cause |
|---|---|
| `BTR_DATABASE_URL is not set` | environment variable missing or not saved (step 7) |
| connection timeout | outbound IPs not in the database firewall (step 8) |
| `password authentication failed` | wrong app password in `db-url`, or `create-app-role` (step 3) not run |

## 10. Sign-in with Entra ID (needs IT)

Ask IT for an app registration with:

- single tenant (this directory only), which includes B2B guest accounts such
  as SLOCAT colleagues;
- redirect URI `https://<app-url>/.auth/login/aad/callback`;
- a client secret;
- *Assignment required* on the enterprise application, with a security group
  (e.g. "BTR Review Users") assigned and you as its owner, so you decide who
  gets in. Alternatively, IT gives you the Application Developer role and you
  register the app yourself.

With the client ID, tenant ID and secret from IT:

1. `btr-review` → **Settings → Authentication** → *Add identity provider* →
   Microsoft → *Provide the details of an existing app registration*. Issuer
   URL: `https://login.microsoftonline.com/<tenant-id>/v2.0`.
   Restrict access: *Require authentication*; unauthenticated requests:
   *HTTP 302 Found redirect*.
2. **Containers** → *Edit and deploy* → set `BTR_EASY_AUTH` to `1` → Create.
3. **Ingress** → remove the IP restriction from step 6.

From then on decisions are stored under the signed-in account (usually the
e-mail address) instead of typed initials. Earlier decisions keep their
initials.

Whether a SLOCAT guest can sign in also depends on GIZ's conditional-access
rules for guests (e.g. MFA). Test with one guest first.

## 11. Updating the app

1. If the release changes the database schema (its notes say so), first run
   on your machine: `python pipeline.py --db $ADMIN_URL init-db`
2. Push to GitHub, then in Cloud Shell:
   `az acr build -r transportpolicyminer -t btr-review:<next number> https://github.com/Changing-Transport/btr-analysis.git#main`
3. `btr-review` → **Containers** → *Edit and deploy* → container → image tag
   `<next number>` → Create.

## Working with the hosted database

Once the app runs on Azure, the PostgreSQL database is the master copy of all
review work. Pipeline commands take the admin URL:

```powershell
python pipeline.py --db $ADMIN_URL stats
python pipeline.py --db $ADMIN_URL match --missing-only
python pipeline.py --db $ADMIN_URL export --out btr_analysis_export.xlsx
```

Or put `BTR_DATABASE_URL=<admin URL>` into `.env` (gitignored) and drop
`--db`. Each command prints which database it uses, with the password masked.

Stop committing review decisions through `data/btr_analysis.db` at that point:
the snapshot in git becomes a historical copy, and the local review app keeps
working on SQLite for offline tests only.

Backups: the server keeps automatic backups for 7 days; restore to a point in
time from the server's **Backup and restore** page.
