#Requires -Version 7
<#
.SYNOPSIS
Create .env in the current directory with new random secrets for docker compose (Windows).

.DESCRIPTION
Run it once, in the directory that holds docker-compose.yml:

    pwsh tools/generate-env.ps1

It never overwrites or changes an existing .env: it refuses if anything already
exists at that path. The file is created with an access list that grants only
the current user. On Linux and macOS use tools/generate-env.sh instead.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Fail([string] $Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

if (-not $IsWindows) {
    Fail 'This script is for Windows. On Linux and macOS run: sh tools/generate-env.sh'
}

$names = @('ADMIN_API_KEY', 'SESSION_SECRET', 'APP_ENCRYPTION_KEY', 'REDIS_PASSWORD')
$out = Join-Path (Get-Location).ProviderPath '.env'

$file = [System.IO.FileInfo]::new($out)
if ($file.Exists -or [System.IO.Directory]::Exists($out) -or $null -ne $file.LinkTarget) {
    Fail '.env already exists, so nothing was changed. To replace one value in it, edit the file and use a new value from: [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()'
}

# Inside a git work tree, the result must be ignored so it cannot be committed by accident.
if (Get-Command git -ErrorAction SilentlyContinue) {
    git rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        git check-ignore -q -- '.env'
        if ($LASTEXITCODE -ne 0) {
            Fail "Git does not ignore '.env' in this directory. Add '.env' and '.env.*' to .gitignore first. Nothing was changed."
        }
    }
}

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add('# Created by tools/generate-env.ps1. Keep this file private; never commit it.')
$lines.Add('# Optional settings: see .env.example.')
foreach ($name in $names) {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $value = [Convert]::ToHexString($bytes).ToLowerInvariant()
    if ($value -notmatch '^[0-9a-f]{64}$') {
        Fail 'Could not generate 32 random bytes. Nothing was changed.'
    }
    $lines.Add("$name=$value")
}
$content = ($lines -join "`n") + "`n"

# Access list applied at creation: only the current user, nothing inherited.
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$security = [System.Security.AccessControl.FileSecurity]::new()
$security.SetAccessRuleProtection($true, $false)
$security.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $user,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow))

try {
    # CreateNew fails if anything appeared at the path in the meantime.
    $stream = [System.IO.FileSystemAclExtensions]::Create(
        $file,
        [System.IO.FileMode]::CreateNew,
        [System.Security.AccessControl.FileSystemRights]::Write,
        [System.IO.FileShare]::None,
        4096,
        [System.IO.FileOptions]::None,
        $security)
}
catch [System.IO.IOException] {
    Fail '.env appeared while the secrets were generated. Nothing was changed.'
}

try {
    $writer = [System.IO.StreamWriter]::new($stream, [System.Text.UTF8Encoding]::new($false))
    $writer.Write($content)
    $writer.Flush()
}
finally {
    if ($null -ne (Get-Variable writer -ValueOnly -ErrorAction SilentlyContinue)) { $writer.Dispose() } else { $stream.Dispose() }
}

Write-Output "Created .env with new random values for: $($names -join ' ')"
Write-Output 'Add optional settings from .env.example if you need them, then run: docker compose up -d'
