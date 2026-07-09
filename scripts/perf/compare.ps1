param(
    [Parameter(Mandatory=$true)][string]$Baseline,
    [Parameter(Mandatory=$true)][string]$Candidate
)

$ErrorActionPreference = "Stop"
$base = Get-Content -LiteralPath $Baseline -Raw | ConvertFrom-Json
$cand = Get-Content -LiteralPath $Candidate -Raw | ConvertFrom-Json
[pscustomobject]@{
    baseline = $Baseline
    candidate = $Candidate
    sameProfile = ($base.profileId -eq $cand.profileId)
    baselineStatus = $base.status
    candidateStatus = $cand.status
} | ConvertTo-Json -Depth 6
