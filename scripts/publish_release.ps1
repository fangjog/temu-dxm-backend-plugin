$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Write-Host "GitHub CLI gh is not installed. Install it first: https://cli.github.com/" -ForegroundColor Red
  exit 1
}

gh auth status

$Version = (Get-Content VERSION -Raw).Trim()
$Repo = "temu-dxm-backend-plugin"
$Owner = gh api user --jq ".login"
$env:GITHUB_OWNER = $Owner

python scripts/package_backend_plugin.py

$Zip = Join-Path $Root "dist\temu_dxm_backend_plugin_v$Version.zip"
$Latest = Join-Path $Root "dist\latest.json"

if (-not (Test-Path $Zip)) {
  Write-Host "Missing zip: $Zip. Run python scripts/package_backend_plugin.py first." -ForegroundColor Red
  exit 1
}
if (-not (Test-Path $Latest)) {
  Write-Host "Missing latest.json: $Latest. Run python scripts/package_backend_plugin.py first." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path ".git")) {
  git init
  git branch -M main
}

git status --short
git add app browser scripts README.md CHANGELOG.md VERSION update_config.json .gitignore
git commit -m "Release v$Version" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "No new code changes to commit or commit failed; continuing to release step."
}

$Remote = ""
try { $Remote = git remote get-url origin } catch {}
if (-not $Remote) {
  $Exists = $false
  try { gh repo view "$Owner/$Repo" *> $null; $Exists = $true } catch {}
  if (-not $Exists) {
    gh repo create $Repo --private --source=. --remote=origin --push
  } else {
    git remote add origin "https://github.com/$Owner/$Repo.git"
    git push -u origin main
  }
} else {
  git push -u origin main
}

$Tag = "v$Version"
git tag -f $Tag
git push origin $Tag --force

$ExistingRelease = $false
try { gh release view $Tag *> $null; $ExistingRelease = $true } catch {}
if ($ExistingRelease) {
  gh release upload $Tag $Zip $Latest --clobber
} else {
  gh release create $Tag $Zip $Latest --title $Tag --notes "Initial portable plugin release"
}

gh release view $Tag --web
