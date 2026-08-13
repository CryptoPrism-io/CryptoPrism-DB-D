# CP-011 Stage 2B — assemble the AWS worker bundle from validated commit files.
# Copies only the files the runner needs (pit package + TA modules) into
# aws/cp011-stage2b/app/ so the bundle can be zipped/scp'd to the EC2/Fargate
# worker. Run from the repo root:  powershell -File aws/cp011-stage2b/package.ps1
$ErrorActionPreference = "Stop"
$root = "C:\cpio_db\CryptoPrism-DB-cp011c"
$bundle = Join-Path $root "aws\cp011-stage2b\app"
$pitSrc = Join-Path $root "gcp_postgres_sandbox\pit"
$taSrc = Join-Path $root "gcp_postgres_sandbox\technical_analysis"
$pitDst = Join-Path $bundle "gcp_postgres_sandbox\pit"
$taDst = Join-Path $bundle "gcp_postgres_sandbox\technical_analysis"
New-Item -ItemType Directory -Force -Path $pitDst, $taDst | Out-Null

$pitFiles = @(
  "run_v2.py","regenerate.py","policy.py","targets.py","db.py",
  "var_cvar.py","metrics.py","scores.py","universe.py","stage3_validate.py",
  "cp012.py","run_cp012.py","__init__.py"
)
foreach ($f in $pitFiles) {
  Copy-Item (Join-Path $pitSrc $f) (Join-Path $pitDst $f) -Force
}
$taFiles = @("gcp_dmv_mom.py","gcp_dmv_osc.py","gcp_dmv_tvv.py","gcp_dmv_rat.py")
foreach ($f in $taFiles) {
  Copy-Item (Join-Path $taSrc $f) (Join-Path $taDst $f) -Force
}
# run_v2.py asserts shadow schema via pit.targets; copy test? not needed on worker.
Write-Host "bundle assembled -> $bundle"
Get-ChildItem -Recurse $bundle -File | Measure-Object | Select-Object -ExpandProperty Count | ForEach-Object { "files: $_" }
