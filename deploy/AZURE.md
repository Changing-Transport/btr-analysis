# Deploying the review app on Azure

Target setup: the review app runs as its own Container App (`btr-review`) in the
same Container Apps environment as the climate-policy-miner (resource group
`ndc-tracker`), and stores its data in an Azure Database for PostgreSQL
flexible server. The database is meant to be shared later with the Tracker's
own tables (schemas `ndc`, `ref`); the BTR tables live in schema `btr`.

Commands are written for **PowerShell** with the Azure CLI (`az login` first;
`az upgrade` if your CLI is older than a few months). Run them from the
repository root. Values in `<angle brackets>` are yours to fill in.

> Two things are not automated here, on purpose:
> * **Sign-in** needs an app registration in GIZ's Entra ID, which IT has to
>   create (see step 8). Until then, ingress is restricted to your own IP.
> * **Continuous deployment** (GitHub Actions) needs an Azure identity with
>   rights on the resource group. Deploying by hand (step 9) is enough for now.

## 0. Variables

```powershell
$RG       = "ndc-tracker"
$LOCATION = "germanywestcentral"
$ACR      = "transportpolicyminer"
$APP      = "btr-review"
$PG       = "<ndc-tracker-db>"     # server name, must be unique across Azure
$ENV_ID   = az containerapp show -n transport-policy-miner -g $RG --query properties.managedEnvironmentId -o tsv
$MY_IP    = Invoke-RestMethod https://api.ipify.org

# Letters and digits only, so the passwords need no escaping inside URLs
function New-Password { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ }) }
$PG_ADMIN_PW = New-Password
$PG_APP_PW   = New-Password
$FLASK_SECRET = New-Password
```

Store the two database passwords in your password manager now. PowerShell
variables are gone when the window closes.

## 1. Database server

Burstable B1ms (1 vCore, 2 GB RAM) is ample for this workload. Check the
current price in the Azure pricing calculator before creating it.

```powershell
az postgres flexible-server create -g $RG -n $PG -l $LOCATION `
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 `
  --admin-user tracker_admin --admin-password $PG_ADMIN_PW `
  --public-access none --backup-retention 7 --high-availability Disabled --yes

az postgres flexible-server db create -g $RG --server-name $PG --database-name tracker
```

`--public-access none` creates a server that is reachable over the internet in
principle but admits no client until a firewall rule allows it. If GIZ's Azure
policies forbid public network access, this step fails; then the server needs
private networking and IT has to be involved.

## 2. Firewall: your own machine

```powershell
az postgres flexible-server firewall-rule create -g $RG --server-name $PG `
  --name allow-nico --start-ip-address $MY_IP --end-ip-address $MY_IP
```

With Azure CLI older than 2.86 the server is `--name $PG` and the rule
`--rule-name allow-nico`. Do **not** use the "allow all Azure services" rule
(0.0.0.0): it admits resources of every Azure customer, not only yours. Your
public IP changes with network and VPN; add a rule again when it does.

## 3. Copy the data

Install the PostgreSQL driver locally once: `pip install -r requirements.txt`.
Stop the local review app, then copy your working copy (it holds the newest
decisions):

```powershell
$ADMIN_URL = "postgresql://tracker_admin:$PG_ADMIN_PW@$PG.postgres.database.azure.com:5432/tracker?sslmode=require"

python pipeline.py --db $ADMIN_URL copy-db --from data/btr_analysis.local.db
```

The command creates the `btr` schema, copies every table with its ids, and
verifies each table row by row. It refuses to write into a database that
already holds data unless you pass `--replace`.

## 4. Login for the app

The app connects with its own role that can read and write rows but cannot
change or drop tables:

```powershell
$env:BTR_APP_DB_PASSWORD = $PG_APP_PW
python pipeline.py --db $ADMIN_URL create-app-role
Remove-Item Env:BTR_APP_DB_PASSWORD
```

## 5. Build the image

```powershell
az acr build -r $ACR -t btr-review:1 .
```

The image contains only `btr_pipeline/`, `review_app/` and `wsgi.py` (see
`.dockerignore`); no database file goes into it.

## 6. Create the Container App

```powershell
$ACR_PW  = az acr credential show -n $ACR --query "passwords[0].value" -o tsv
$APP_URL = "postgresql://btr_app:$PG_APP_PW@$PG.postgres.database.azure.com:5432/tracker?sslmode=require"

az containerapp create -n $APP -g $RG --environment $ENV_ID `
  --image "$ACR.azurecr.io/btr-review:1" `
  --registry-server "$ACR.azurecr.io" --registry-username $ACR --registry-password $ACR_PW `
  --target-port 8000 --ingress external `
  --min-replicas 0 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
  --secrets "db-url=$APP_URL" "flask-secret=$FLASK_SECRET" "aoai-key=<AZURE_OPENAI_API_KEY>" `
  --env-vars "BTR_DATABASE_URL=secretref:db-url" "FLASK_SECRET_KEY=secretref:flask-secret" `
             "BTR_EASY_AUTH=0" `
             "AZURE_OPENAI_API_KEY=secretref:aoai-key" `
             "AZURE_OPENAI_ENDPOINT=<endpoint>" "AZURE_OPENAI_API_VERSION=<version>" `
             "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=<embedding deployment>"

# Until sign-in is configured: only your IP may open the app
az containerapp ingress access-restriction set -n $APP -g $RG `
  --rule-name allow-nico --ip-address "$MY_IP/32" --action Allow
```

The Azure OpenAI values are the same as the climate-policy-miner's. They let
the app generate NDC candidates the moment a measure is confirmed
transport-relevant; without them that step falls back to `pipeline.py match`.

`--min-replicas 0` lets the app sleep when unused; the first request after a
pause takes a few seconds.

## 7. Firewall: the app

```powershell
$OUT_IPS = (az containerapp show -n $APP -g $RG --query properties.outboundIpAddresses -o tsv) -split "\s+"
$i = 0
foreach ($ip in $OUT_IPS) {
  if ($ip) {
    $i++
    az postgres flexible-server firewall-rule create -g $RG --server-name $PG `
      --name "btr-review-out-$i" --start-ip-address $ip --end-ip-address $ip
  }
}
```

Check it works:

```powershell
$FQDN = az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv
Invoke-RestMethod "https://$FQDN/healthz"     # -> status: ok
```

If the app later reports database timeouts, compare the outbound IPs again;
they rarely change, but they can.

## 8. Sign-in with Entra ID (needs IT)

Ask IT for an app registration with:

* single tenant (this directory only), which includes B2B guest accounts such
  as SLOCAT colleagues;
* redirect URI `https://<FQDN>/.auth/login/aad/callback`;
* a client secret;
* "Assignment required" on the enterprise application, with a security group
  (e.g. "BTR Review Users") assigned and you as its owner, so you decide who
  gets in. Alternatively, IT gives you the Application Developer role.

With the client ID, tenant ID and secret from IT, in the Azure portal: Container
App `btr-review` → Authentication → Add identity provider → Microsoft →
"Provide the details of an existing app registration". Issuer URL:
`https://login.microsoftonline.com/<TENANT_ID>/v2.0`. Set "Restrict access" to
"Require authentication" and "Unauthenticated requests" to "HTTP 302 Found
redirect".

Then let the app trust the sign-in and lift the IP restriction:

```powershell
az containerapp update -n $APP -g $RG --set-env-vars "BTR_EASY_AUTH=1"
az containerapp ingress access-restriction remove -n $APP -g $RG --rule-name allow-nico
```

From then on decisions are stored under the signed-in account (usually the
e-mail address) instead of typed initials. Earlier decisions keep their
initials.

Guest accounts: whether a SLOCAT guest can sign in also depends on GIZ's
conditional-access rules for guests (e.g. MFA). Test with one guest first.

## 9. Updating

```powershell
# Schema changes first (only when a release says so), with the admin login
python pipeline.py --db $ADMIN_URL init-db

az acr build -r $ACR -t btr-review:2 .
az containerapp update -n $APP -g $RG --image "$ACR.azurecr.io/btr-review:2"
```

## Working with the hosted database

Once the app runs on Azure, the PostgreSQL database is the master copy of all
review work. Pipeline commands take the same URL:

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

Backups: the server keeps automatic daily backups for 7 days
(point-in-time restore in the portal).
