#Requires -Version 5.1
<#
.SYNOPSIS
    Installs and configures RustDesk for UNATTENDED access against a self-hosted
    RustDesk server (hbbs/hbbr) - "Option A": one shared permanent password, no
    API server / user accounts.

.DESCRIPTION
    Run this ON EACH WINDOWS ENDPOINT you want to reach. It will:
      1. Ask for admin rights (UAC) if it doesn't already have them.
      2. Load your server address + public key from deploy-settings.json
         (next to this script) or ask you for them.
      3. Check that the endpoint can reach your server (TCP 21116 / 21117).
      4. Download the latest official RustDesk installer from GitHub, check its
         digital signature (and SHA-256 when GitHub publishes one), and install
         it silently. Skipped if the same version is already installed.
      5. Make sure the RustDesk Windows service exists, is Automatic, and runs.
      6. Point the client at your server and key, set:
            verification-method = use-permanent-password  (no one-time codes)
            approve-mode        = password                (no "Accept" click)
            whitelist           = optional allowed IPs/CIDRs
         ...and read each value back to prove it stuck.
      7. Ask YOU to type the shared permanent password (hidden, typed twice).
         The password is never written to disk, a log, or this script.
      8. Print the RustDesk ID and append it to endpoints.csv (no password).

    Nothing in this script turns off a firewall or opens inbound ports on the
    endpoint. RustDesk makes OUTBOUND connections to your server.

.PARAMETER SettingsPath
    Path to the JSON settings file. Default: deploy-settings.json next to this script.

.PARAMETER InventoryPath
    CSV file to append the endpoint's ID to. Default: endpoints.csv next to this script.

.PARAMETER Notes
    Free text saved in the inventory row (e.g. "Mom - kitchen PC").

.PARAMETER Force
    Reinstall RustDesk even if the latest version is already installed.

.PARAMETER SkipInstall
    Only (re)configure an existing RustDesk install. Useful for password rotation.

.EXAMPLE
    Right-click Deploy-RustDesk.cmd -> Run as administrator

.EXAMPLE
    .\Deploy-RustDesk.ps1 -Notes "Front desk PC" -SkipInstall
#>
[CmdletBinding()]
param(
    [string]$SettingsPath = (Join-Path $PSScriptRoot 'deploy-settings.json'),
    [string]$InventoryPath = (Join-Path $PSScriptRoot 'endpoints.csv'),
    [string]$Notes = '',
    [switch]$Force,
    [switch]$SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # makes Invoke-WebRequest much faster on PS 5.1

# ---------------------------------------------------------------------------
# 0. Elevate if needed
# ---------------------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Administrator rights are needed. A UAC prompt will appear...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', "`"$PSCommandPath`"")
    foreach ($p in $PSBoundParameters.GetEnumerator()) {
        if ($p.Value -is [System.Management.Automation.SwitchParameter]) {
            if ($p.Value.IsPresent) { $argList += "-$($p.Key)" }
        } else {
            $argList += "-$($p.Key)"
            $argList += "`"$($p.Value)`""
        }
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    exit
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$WorkDir     = Join-Path $env:ProgramData 'EBD\RustDesk'
$InstallDir  = Join-Path $env:ProgramFiles 'RustDesk'
$Exe         = Join-Path $InstallDir 'rustdesk.exe'
$ServiceName = 'Rustdesk'
$UninstallKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\RustDesk'

New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
$LogFile = Join-Path $WorkDir ("deploy-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $LogFile -Append | Out-Null   # Read-Host -AsSecureString input is NOT captured

$script:Warnings = New-Object System.Collections.Generic.List[string]

function Write-Step  ([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    ([string]$m) { Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Warn2 ([string]$m) { Write-Host "    [WARN] $m" -ForegroundColor Yellow; $script:Warnings.Add($m) }
function Write-Fail  ([string]$m) { Write-Host "    [FAIL] $m" -ForegroundColor Red }

function Stop-WithError ([string]$m) {
    Write-Fail $m
    Write-Host "`nLog: $LogFile"
    Stop-Transcript | Out-Null
    exit 1
}

function Test-TcpPort ([string]$HostName, [int]$Port, [int]$TimeoutMs = 4000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($ar)
        return $true
    } catch { return $false }
    finally { $client.Close() }
}

# rustdesk.exe is a GUI program. Piping its output forces PowerShell to wait for
# it and lets us capture what it prints (same technique as RustDesk's own docs).
function Invoke-RustDesk ([string[]]$Arguments) {
    $out = & $Exe @Arguments 2>&1 | Out-String
    return $out.Trim()
}

function Set-RustDeskOption ([string]$Name, [string]$Value) {
    Invoke-RustDesk @('--option', $Name, $Value) | Out-Null
    Start-Sleep -Milliseconds 500
    $readBack = Invoke-RustDesk @('--option', $Name)
    if ($readBack -eq $Value) {
        $shown = if ($Name -eq 'key') { $Value.Substring(0, 6) + '...' } else { $Value }
        Write-Ok "$Name = $shown"
        return $true
    }
    Write-Warn2 "$Name did not read back as expected (got '$readBack'). Set it in the RustDesk GUI (see README)."
    return $false
}

function Wait-Until ([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (& $Condition) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Warn2 "Timed out after $Seconds s waiting for: $What"
    return $false
}

Write-Host '============================================================'
Write-Host ' RustDesk unattended-access deployment (Option A)'
Write-Host (" Computer: {0}    {1:yyyy-MM-dd HH:mm}" -f $env:COMPUTERNAME, (Get-Date))
Write-Host '============================================================'

# ---------------------------------------------------------------------------
# 1. Settings
# ---------------------------------------------------------------------------
Write-Step 'Loading settings'
$settings = [ordered]@{ IdServer = ''; RelayServer = ''; Key = ''; AllowedIPs = ''; ConfigString = '' }
if (Test-Path -LiteralPath $SettingsPath) {
    $json = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    foreach ($k in @($settings.Keys)) {
        if ($json.PSObject.Properties.Name -contains $k -and $json.$k) { $settings[$k] = [string]$json.$k }
    }
    Write-Ok "Read $SettingsPath"
} else {
    Write-Warn2 "No settings file at $SettingsPath - you'll be asked for the values."
}

if (-not $settings.IdServer)    { $settings.IdServer = (Read-Host '    RustDesk ID server hostname (e.g. rustdesk.example.com)').Trim() }
if (-not $settings.RelayServer) { $settings.RelayServer = $settings.IdServer }
if (-not $settings.Key)         { $settings.Key = (Read-Host '    Server public key (contents of id_ed25519.pub)').Trim() }

if ($settings.IdServer -notmatch '^[A-Za-z0-9.\-]+(:\d+)?$') { Stop-WithError "ID server '$($settings.IdServer)' doesn't look like a hostname." }
if ($settings.Key -notmatch '^[A-Za-z0-9+/]{43}=$')           { Stop-WithError 'Server key should be 44 characters ending in "=". Copy it again from id_ed25519.pub.' }
$serverHost = ($settings.IdServer -split ':')[0]
$relayHost  = ($settings.RelayServer -split ':')[0]
Write-Ok "ID server: $($settings.IdServer)   Relay: $($settings.RelayServer)   Key: $($settings.Key.Substring(0,6))..."

# ---------------------------------------------------------------------------
# 2. Can this PC reach the server?
# ---------------------------------------------------------------------------
Write-Step 'Checking that this PC can reach your RustDesk server'
try {
    $ips = [System.Net.Dns]::GetHostAddresses($serverHost) | ForEach-Object { $_.IPAddressToString }
    Write-Ok "$serverHost resolves to $($ips -join ', ')"
} catch {
    Write-Warn2 "DNS lookup for $serverHost failed."
}
$reach = $true
foreach ($t in @(@{H = $serverHost; P = 21116; N = 'ID server (hbbs)'}, @{H = $relayHost; P = 21117; N = 'relay (hbbr)'})) {
    if (Test-TcpPort $t.H $t.P) { Write-Ok "TCP $($t.H):$($t.P) $($t.N) reachable" }
    else { Write-Warn2 "TCP $($t.H):$($t.P) $($t.N) NOT reachable"; $reach = $false }
}
Write-Host '    (UDP 21116 is also used but can''t be tested from here.)'
if (-not $reach) {
    $ans = Read-Host '    The server could not be reached. Continue anyway? (y/N)'
    if ($ans -notmatch '^[Yy]') { Stop-WithError 'Stopped at your request. Fix connectivity (DNS / port forwards) and run again.' }
}

# ---------------------------------------------------------------------------
# 3. Download + verify + install
# ---------------------------------------------------------------------------
$installed = $null
if (Test-Path $UninstallKey) { $installed = (Get-ItemProperty $UninstallKey -ErrorAction SilentlyContinue).DisplayVersion }

if ($SkipInstall) {
    Write-Step 'Skipping install (-SkipInstall)'
    if (-not (Test-Path $Exe)) { Stop-WithError "RustDesk isn't installed at $Exe. Run again without -SkipInstall." }
    Write-Ok "Installed version: $installed"
} else {
    Write-Step 'Finding the latest official RustDesk release on GitHub'
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/rustdesk/rustdesk/releases/latest' `
                                 -Headers @{ 'User-Agent' = 'rustdesk-deploy-script' } -UseBasicParsing
    } catch { Stop-WithError "Couldn't query GitHub: $($_.Exception.Message)" }
    $asset = $rel.assets | Where-Object { $_.name -match '^rustdesk-[\d.]+-x86_64\.exe$' } | Select-Object -First 1
    if (-not $asset) { Stop-WithError "Release $($rel.tag_name) has no x86_64 .exe installer." }
    $latest = $rel.tag_name.TrimStart('v')
    Write-Ok "Latest: $latest ($($asset.name))   Installed: $(if ($installed) { $installed } else { 'none' })"
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { Write-Warn2 'ARM64 Windows: the x86_64 build runs under emulation.' }

    if ($installed -eq $latest -and -not $Force) {
        Write-Ok 'Already on the latest version - skipping download/install (use -Force to reinstall).'
    } else {
        Write-Step "Downloading $($asset.name)"
        $dlDir = Join-Path $WorkDir 'download'
        if (Test-Path $dlDir) { Remove-Item $dlDir -Recurse -Force }
        New-Item -ItemType Directory -Path $dlDir | Out-Null
        $installer = Join-Path $dlDir $asset.name
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -UseBasicParsing
        Write-Ok ("Downloaded {0:N1} MB" -f ((Get-Item $installer).Length / 1MB))

        # SHA-256 check when GitHub publishes a digest for the asset
        if ($asset.PSObject.Properties.Name -contains 'digest' -and $asset.digest -match '^sha256:([0-9a-fA-F]{64})$') {
            $hash = (Get-FileHash $installer -Algorithm SHA256).Hash
            if ($hash -ne $Matches[1].ToUpper()) { Stop-WithError 'SHA-256 does not match GitHub. Download may be corrupted or tampered with.' }
            Write-Ok 'SHA-256 matches GitHub'
        } else {
            Write-Host '    (GitHub published no SHA-256 for this file; relying on the signature check.)'
        }

        $sig = Get-AuthenticodeSignature -FilePath $installer
        if ($sig.Status -ne 'Valid') { Stop-WithError "Installer signature is '$($sig.Status)'. Not installing." }
        Write-Ok "Signed by: $($sig.SignerCertificate.Subject)"

        Write-Step 'Installing silently'
        # Not using Start-Process -Wait: on Windows PowerShell 5.1 it also waits for
        # child processes, and the installer leaves RustDesk running -> it would hang.
        $proc = Start-Process -FilePath $installer -ArgumentList '--silent-install' -PassThru
        if (-not $proc.WaitForExit(180000)) { Write-Warn2 'Installer still running after 3 minutes - continuing.' }
        Wait-Until { Test-Path $Exe } 120 'rustdesk.exe to appear' | Out-Null
        Wait-Until { [bool](Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) } 60 'RustDesk service to be created' | Out-Null
        Remove-Item $dlDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $Exe)) { Stop-WithError "Install didn't produce $Exe." }
        Write-Ok "Installed to $InstallDir"
    }
}

# ---------------------------------------------------------------------------
# 4. Service
# ---------------------------------------------------------------------------
Write-Step 'Making sure the RustDesk service runs at boot'
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Invoke-RustDesk @('--install-service') | Out-Null
    Wait-Until { [bool](Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) } 60 'RustDesk service to be created' | Out-Null
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
}
if (-not $svc) { Stop-WithError 'RustDesk service is missing. Open RustDesk and click "Install service", then re-run with -SkipInstall.' }
Set-Service -Name $ServiceName -StartupType Automatic
if ($svc.Status -ne 'Running') { Start-Service -Name $ServiceName }
Wait-Until { (Get-Service -Name $ServiceName).Status -eq 'Running' } 30 'service to start' | Out-Null
Start-Sleep -Seconds 3
Write-Ok "Service '$ServiceName' is $((Get-Service -Name $ServiceName).Status), startup Automatic"

# ---------------------------------------------------------------------------
# 5. Server + security options
# ---------------------------------------------------------------------------
Write-Step 'Pointing RustDesk at your server and locking down unattended access'
if ($settings.ConfigString) {
    Invoke-RustDesk @('--config', $settings.ConfigString) | Out-Null
    Write-Ok 'Applied exported config string'
}
$allOk = $true
$allOk = (Set-RustDeskOption 'custom-rendezvous-server' $settings.IdServer)    -and $allOk
$allOk = (Set-RustDeskOption 'relay-server'             $settings.RelayServer) -and $allOk
$allOk = (Set-RustDeskOption 'key'                      $settings.Key)         -and $allOk
$allOk = (Set-RustDeskOption 'verification-method'      'use-permanent-password') -and $allOk
$allOk = (Set-RustDeskOption 'approve-mode'             'password')            -and $allOk
if ($settings.AllowedIPs) {
    $allOk = (Set-RustDeskOption 'whitelist' $settings.AllowedIPs) -and $allOk
} else {
    Write-Host '    (No AllowedIPs in settings - IP whitelist left off.)'
}

# ---------------------------------------------------------------------------
# 6. Permanent password (typed by a human, never stored)
# ---------------------------------------------------------------------------
Write-Step 'Set the shared permanent password'
Write-Host '    Paste it from Vaultwarden. Typing is hidden. It is NOT saved anywhere by this script.'
$plain = $null
for ($try = 1; $try -le 3; $try++) {
    $s1 = Read-Host '    Password' -AsSecureString
    $s2 = Read-Host '    Again   ' -AsSecureString
    $b1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s1)
    $b2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s2)
    try {
        $p1 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b1)
        $p2 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b2)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b1)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2)
    }
    if ($p1 -cne $p2)          { Write-Fail 'The two entries did not match.'; continue }
    if ($p1.Length -lt 12)     { Write-Fail 'Use at least 12 characters (16+ recommended).'; continue }
    if ($p1 -match '["\s]')    { Write-Fail 'Please avoid spaces and double quotes (") - they break command-line passing.'; continue }
    $plain = $p1
    break
}
$p1 = $null; $p2 = $null
if (-not $plain) { Stop-WithError 'Password not set after 3 attempts.' }
$pwOut = Invoke-RustDesk @('--password', $plain)
$plain = $null
[GC]::Collect()
if ($pwOut) { Write-Ok "RustDesk replied: $pwOut" } else { Write-Ok 'Permanent password set' }

# Restart so the service definitely picks up every change
Restart-Service -Name $ServiceName
Wait-Until { (Get-Service -Name $ServiceName).Status -eq 'Running' } 30 'service to restart' | Out-Null
Start-Sleep -Seconds 5

# ---------------------------------------------------------------------------
# 7. ID + inventory
# ---------------------------------------------------------------------------
Write-Step 'Reading this PC''s RustDesk ID'
$id = ''
for ($i = 0; $i -lt 30 -and $id -notmatch '^\d{6,}$'; $i++) {
    $id = Invoke-RustDesk @('--get-id')
    if ($id -notmatch '^\d{6,}$') { Start-Sleep -Seconds 1 }
}
if ($id -notmatch '^\d{6,}$') { Write-Warn2 "Couldn't read the ID (got '$id'). Open RustDesk to see it."; $id = 'UNKNOWN' }
else { Write-Ok "RustDesk ID: $id" }

$version = if (Test-Path $UninstallKey) { (Get-ItemProperty $UninstallKey).DisplayVersion } else { '' }
$os = (Get-CimInstance Win32_OperatingSystem).Caption
$row = [pscustomobject]@{
    Date         = (Get-Date -Format 'yyyy-MM-dd HH:mm')
    ComputerName = $env:COMPUTERNAME
    RustDeskId   = $id
    Version      = $version
    OS           = $os
    Notes        = $Notes
}
try {
    $row | Export-Csv -LiteralPath $InventoryPath -Append -NoTypeInformation -Encoding UTF8
    Write-Ok "Added to $InventoryPath"
} catch {
    $fallback = Join-Path $WorkDir 'endpoints.csv'
    $row | Export-Csv -LiteralPath $fallback -Append -NoTypeInformation -Encoding UTF8
    Write-Warn2 "Couldn't write $InventoryPath; saved to $fallback instead."
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "`n============================================================"
if ($script:Warnings.Count -eq 0 -and $allOk) {
    Write-Host ' DONE - this PC is ready for unattended access.' -ForegroundColor Green
} else {
    Write-Host " DONE WITH $($script:Warnings.Count) WARNING(S):" -ForegroundColor Yellow
    $script:Warnings | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
}
Write-Host " Computer : $env:COMPUTERNAME"
Write-Host " ID       : $id"
Write-Host ' Next     : from your PC, connect to this ID with the shared password,'
Write-Host '            tick "Remember password", then reboot this PC and test again'
Write-Host '            WITHOUT logging in (you should see the Windows sign-in screen).'
Write-Host " Log      : $LogFile"
Write-Host '============================================================'
Stop-Transcript | Out-Null
