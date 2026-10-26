param(
    [Parameter(Mandatory = $true)]
    [string]$LanIPv4
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$mkcertCommand = Get-Command mkcert -ErrorAction SilentlyContinue
if (-not $mkcertCommand) {
    throw "mkcert is not installed. Install mkcert, reopen PowerShell, then run this script again."
}
if ($LanIPv4 -notmatch '^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$' -or $LanIPv4.StartsWith("169.254.")) {
    throw "Enter the computer's Wi-Fi IPv4 address from ipconfig, for example 192.168.1.23."
}

$certPath = Join-Path $root "commute-local-cert.pem"
$keyPath = Join-Path $root "commute-local-key.pem"
$phoneSetupDir = Join-Path $root "phone-setup"
$rootCaPath = Join-Path $phoneSetupDir "commute-root-ca.pem"

Write-Host "Installing mkcert's local development CA into this Windows account..."
& $mkcertCommand.Source -install
if ($LASTEXITCODE -ne 0) { throw "mkcert -install failed." }

Write-Host "Creating a certificate for localhost and $LanIPv4..."
& $mkcertCommand.Source -cert-file $certPath -key-file $keyPath localhost 127.0.0.1 ::1 $LanIPv4
if ($LASTEXITCODE -ne 0) { throw "Certificate generation failed." }

$caRoot = (& $mkcertCommand.Source -CAROOT | Select-Object -Last 1).Trim()
$sourceRootCa = Join-Path $caRoot "rootCA.pem"
if (-not (Test-Path -LiteralPath $sourceRootCa)) { throw "mkcert root CA certificate was not found." }
New-Item -ItemType Directory -Path $phoneSetupDir -Force | Out-Null
Copy-Item -LiteralPath $sourceRootCa -Destination $rootCaPath -Force

Write-Host "HTTPS files are ready in: $root"
Write-Host "Public CA certificate for iPhone: $rootCaPath"
Write-Host "Keep commute-local-key.pem and mkcert's rootCA-key.pem private. Do not upload or send either key file."
Write-Host "Restart the commute web helper. It will serve HTTPS on port 8765."
