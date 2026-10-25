# Solomon Leadgen: build and deploy the outreach engine to Cloud Run.
# ASCII-ONLY (Windows PowerShell 5.1 reads BOM-less scripts as Windows-1252).
#
#   .\deploy\deploy.ps1                       # everything
#   .\deploy\deploy.ps1 -Account deployer@your-gcp-project.iam.gserviceaccount.com
#
# One image, four entrypoints:
#   solomon-leadgen        Cloud Run Job   health | prospect | tick (each scheduler job sets its args)
#   solomon-leadgen-api    Cloud Run svc   PRIVATE engine API; run.invoker only for creator-outreach's account
#   solomon-leadgen-unsub  Cloud Run svc   PUBLIC signed one-click unsubscribe
# Safe to re-run: every step creates-if-missing or updates in place. Nothing can
# email a prospect until the operator launches a campaign in The Creator.
param(
  [string]$Account = "",            # gcloud account to act as (blank = active account)
  [string]$RuntimeSa = "",          # engine runtime SA (blank = solomon-leadgen SA if it exists/can be made, else compute default)
  [string]$OutreachSa = "",         # creator-outreach runtime SA (blank = read from the deployed service)
  [string]$Repo = "solomon"         # Artifact Registry repo; falls back to the existing "creator" repo
)
$ErrorActionPreference = "Stop"
$Project = "your-gcp-project"
$Region  = "us-central1"
$Job     = "solomon-leadgen"
$Api     = "solomon-leadgen-api"
$Unsub   = "solomon-leadgen-unsub"
$Root    = Split-Path $PSScriptRoot -Parent
$Gcloud  = (Get-Command gcloud -ErrorAction SilentlyContinue).Source
if (-not $Gcloud) { $Gcloud = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" }
$Acct = @(); if ($Account) { $Acct = @("--account=$Account") }

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function G {
  # gcloud writes progress to stderr; under Stop that would throw in PS 5.1. Judge the exit code instead.
  $ErrorActionPreference = "Continue"
  & $Gcloud @args @Acct --project $Project 2>&1 | ForEach-Object { "$_" }
  if ($LASTEXITCODE -ne 0) { throw "gcloud exited $LASTEXITCODE : gcloud $($args -join ' ')" }
}
# PS 5.1: with ErrorActionPreference=Stop, a native command writing to stderr throws.
# Probes expect failures, so they run with Continue and judge only the exit code.
function Try-G { $ErrorActionPreference = "Continue"; & $Gcloud @args @Acct --project $Project 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
function Out-G { $ErrorActionPreference = "Continue"; $o = & $Gcloud @args @Acct --project $Project 2>$null; if ($LASTEXITCODE -ne 0) { return "" }; return ($o | Out-String).Trim() }

# The image tag must describe a commit (same rule as The Creator's Get-CleanSha).
$dirty = & git -C $Root status --porcelain
if ($dirty) { throw "Working tree is dirty in $Root. Commit before deploying.`n$dirty" }
$Sha   = (& git -C $Root rev-parse --short HEAD).Trim()

Step "Runtime service account"
$ProjectNumber = Out-G projects describe $Project --format "value(projectNumber)"
$ComputeSa = "$ProjectNumber-compute@developer.gserviceaccount.com"
$Dedicated = "solomon-leadgen@$Project.iam.gserviceaccount.com"
if (-not $RuntimeSa) {
  # The dedicated account exists (2026-10-25) but holds no project roles yet; deploying as it
  # left the unsubscribe service unable to read its signing key. Use it only once it can read secrets.
  $policy = Out-G projects get-iam-policy $Project --flatten "bindings[].members" --filter "bindings.role:roles/secretmanager.secretAccessor" --format "value(bindings.members)"
  $dedicatedReady = $policy -match [regex]::Escape("serviceAccount:$Dedicated")
  if ((Try-G iam service-accounts describe $Dedicated) -and $dedicatedReady) { $RuntimeSa = $Dedicated }
  elseif (Try-G iam service-accounts describe $Dedicated) {
    $RuntimeSa = $ComputeSa
    Write-Host "$Dedicated exists but has no Secret Manager access yet; using $ComputeSa." -ForegroundColor Yellow
  }
  elseif (Try-G iam service-accounts create solomon-leadgen --display-name "Solomon Leadgen") { $RuntimeSa = $Dedicated }
  else {
    $RuntimeSa = $ComputeSa
    Write-Host "Could not create $Dedicated with this account; using $ComputeSa (see the deploy notes)." -ForegroundColor Yellow
  }
}
Write-Host "Engine runs as $RuntimeSa"
foreach ($role in "roles/secretmanager.secretAccessor", "roles/secretmanager.viewer", "roles/datastore.user", "roles/run.invoker") {
  if (-not (Try-G projects add-iam-policy-binding $Project --member "serviceAccount:$RuntimeSa" --role $role --condition None)) {
    Write-Host "Could not grant $role to $RuntimeSa with this account (needs project IAM admin)." -ForegroundColor Yellow
  }
}

Step "Unsubscribe signing key (created once, never printed)"
if (-not (Try-G secrets describe global-leadgen-hmac)) {
  $bytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $tmp = New-TemporaryFile
  [IO.File]::WriteAllText($tmp, [Convert]::ToBase64String($bytes))
  $made = Try-G secrets create global-leadgen-hmac --data-file $tmp --replication-policy automatic
  Remove-Item $tmp
  if (-not $made) {
    # This account can't create secrets: let Cloud Build's service account make it inside the build.
    Write-Host "Creating global-leadgen-hmac through Cloud Build" -ForegroundColor Yellow
    G builds submit --no-source --config "$Root\deploy\hmac-cloudbuild.yaml" --quiet
  }
}

Step "Artifact Registry repo"
if (-not (Try-G artifacts repositories describe $Repo --location $Region)) {
  if (-not (Try-G artifacts repositories create $Repo --repository-format docker --location $Region)) {
    Write-Host "Could not create repo '$Repo' with this account; using the existing 'creator' repo." -ForegroundColor Yellow
    $Repo = "creator"
  }
}
$Image = "$Region-docker.pkg.dev/$Project/$Repo/leadgen:$Sha"

Step "Build $Image"
G builds submit $Root --tag $Image --quiet

Step "Unsubscribe service (public; it only accepts signed links)"
G run deploy $Unsub --image $Image --region $Region --service-account $RuntimeSa `
  --args serve --allow-unauthenticated --min-instances 0 --max-instances 3 --memory 256Mi --quiet
$UnsubUrl = Out-G run services describe $Unsub --region $Region --format "value(status.url)"

Step "Engine API (private)"
G run deploy $Api --image $Image --region $Region --service-account $RuntimeSa `
  --args serve-api --no-allow-unauthenticated --min-instances 0 --max-instances 2 --memory 1Gi `
  --timeout 900 --set-env-vars "LEADGEN_UNSUB_URL=$UnsubUrl" --quiet
$ApiUrl = Out-G run services describe $Api --region $Region --format "value(status.url)"
if (-not $OutreachSa) {
  $OutreachSa = Out-G run services describe creator-outreach --region $Region --format "value(spec.template.spec.serviceAccountName)"
  if (-not $OutreachSa) { $OutreachSa = $ComputeSa }
}
G run services add-iam-policy-binding $Api --region $Region --member "serviceAccount:$OutreachSa" --role roles/run.invoker --quiet
Write-Host "Engine API invokable by $OutreachSa"

Step "Job (no retries: a failed run can never double-send)"
G run jobs deploy $Job --image $Image --region $Region --service-account $RuntimeSa `
  --args health --task-timeout 3600 --max-retries 0 --memory 1Gi `
  --set-env-vars "LEADGEN_UNSUB_URL=$UnsubUrl" --quiet

Step "Schedules (America/New_York; each sets its own args)"
$Uri = "https://run.googleapis.com/v2/projects/$Project/locations/$Region/jobs/${Job}:run"
$Schedules = @(
  @{ Name = "solomon-leadgen-health";   Cron = "47 5 * * *";       Arg = "health" },
  @{ Name = "solomon-leadgen-prospect"; Cron = "17 6 * * *";       Arg = "prospect" },
  @{ Name = "solomon-leadgen-tick";     Cron = "*/10 8-19 * * *";  Arg = "tick" }
)
foreach ($s in $Schedules) {
  $body = '{"overrides":{"containerOverrides":[{"args":["' + $s.Arg + '"]}]}}'
  $bodyFile = New-TemporaryFile
  [IO.File]::WriteAllText($bodyFile, $body)
  # create takes --headers; update takes --update-headers.
  if (Try-G scheduler jobs describe $s.Name --location $Region) { $verb = "update"; $hdr = "--update-headers" }
  else { $verb = "create"; $hdr = "--headers" }
  G scheduler jobs $verb http $s.Name --location $Region --schedule $s.Cron --time-zone "America/New_York" `
    --uri $Uri --http-method POST --message-body-from-file $bodyFile $hdr "Content-Type=application/json" `
    --oauth-service-account-email $RuntimeSa --attempt-deadline 600s --quiet
  Remove-Item $bodyFile
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Engine API:   $ApiUrl   (private)"
Write-Host "Unsubscribe:  $UnsubUrl/health   (Cloud Run intercepts /healthz itself)"
Write-Host "Dashboard: point your operator surface at LEADGEN_API_URL=$ApiUrl"
Write-Host "Run a health check now:  gcloud run jobs execute $Job --args=health --region $Region --wait"
