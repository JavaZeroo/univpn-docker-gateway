$ErrorActionPreference = 'Stop'
$script:ImportSideEffects=0
function Get-Service { $script:ImportSideEffects++; throw 'Services must not be queried while importing.' }
function Start-Process { $script:ImportSideEffects++; throw 'Processes must not start while importing.' }
. (Join-Path $PSScriptRoot '../scripts/windows-prerequisites.ps1')
$script:RealInstall = ${function:Invoke-UVPNPrerequisiteInstall}
$script:RealElevated = ${function:Invoke-UVPNPrerequisiteElevatedInstall}
$script:RealArchitecture = ${function:Get-UVPNPrerequisiteArchitecture}
$script:Passed = 0
$script:Fixture = [Text.Encoding]::ASCII.GetBytes('independent MSI fixture, never executable')
$script:UVPNPrerequisiteMsiSize = $script:Fixture.Length
$sha = [Security.Cryptography.SHA256]::Create()
try { $script:UVPNPrerequisiteMsiSha256 = ([BitConverter]::ToString($sha.ComputeHash($script:Fixture))).Replace('-','').ToLowerInvariant() }
finally { $sha.Dispose() }
$script:TestParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\','/')
$script:TestRoot = Join-Path $script:TestParent ('univpn-prereq-tests-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $script:TestRoot

function Assert-True([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw ('Assertion failed: ' + $Message) }
}

function Test-Case([string]$Name,[scriptblock]$Action) {
    & $Action
    $script:Passed++
    Write-Host ('PASS ' + $Name)
}

function Assert-Fails([scriptblock]$Action,[string]$Message='') {
    $caught = $null
    try { & $Action | Out-Null }
    catch { $caught = $_ }
    Assert-True ($null -ne $caught) 'operation should fail'
    if ($Message) { Assert-True ($caught.Exception.Message -like ('*' + $Message + '*')) 'failure reason' }
    return $caught
}

function New-State([string]$Kind) {
    $state = [pscustomobject]@{WireGuardExe='C:\fixture\WireGuard\wireguard.exe';WgExe='C:\fixture\WireGuard\wg.exe';
        WireGuardExists=$false;WgExists=$false;DirectoryExists=$false;ServicesExist=$false;Registered=$false;CustomPathExists=$false}
    switch ($Kind) {
        'Existing' { $state.WireGuardExists=$true;$state.WgExists=$true;$state.DirectoryExists=$true }
        'Partial' { $state.WireGuardExists=$true;$state.DirectoryExists=$true }
        'Directory' { $state.DirectoryExists=$true }
        'Service' { $state.ServicesExist=$true }
        'Registered' { $state.Registered=$true }
        'Custom' { $state.CustomPathExists=$true }
    }
    return $state
}

function Reset-Fixture([string[]]$Sequence=@('Absent','Absent','Absent','Absent','Existing')) {
    $script:States=@($Sequence | ForEach-Object { New-State $_ });$script:StateIndex=0
    $script:Downloads=0;$script:Processes=@();$script:Signatures=@();$script:Temporary=$null
    $script:DownloadMode='Good';$script:SignatureMode='Good';$script:InstallerCode=0
    $script:Admin=$false;$script:Architecture='X64';$script:ProcessThrows=$false
    $script:ChangeDuringUac=$false;$script:InstallSource=$null;$script:ProtectedPath=$null
}

function Get-UVPNPrerequisiteState {
    $index = [Math]::Min($script:StateIndex,$script:States.Count-1)
    $script:StateIndex++
    return $script:States[$index]
}
function Get-UVPNPrerequisiteArchitecture { return $script:Architecture }
function Test-UVPNPrerequisiteAdministrator { return $script:Admin }

function New-UVPNPrerequisiteTemp {
    $name='univpn-wg-'+[Guid]::NewGuid().ToString('N')
    $path=Join-Path $script:TestParent $name
    $null=New-Item -ItemType Directory -Path $path
    $script:Temporary=[pscustomobject]@{Parent=$script:TestParent;Path=$path;Name=$name}
    return $script:Temporary
}
function New-UVPNPrerequisiteAdminTemp { return New-UVPNPrerequisiteTemp }
function New-UVPNPrerequisiteAdminFile([string]$Path) {
    # Unit tests exercise real copy/verification using a normal test file. ACL
    # construction is checked separately; actual administrator creation is CI's job.
    $script:ProtectedPath=$Path
    return [IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
}
function Get-UVPNPrerequisitePackage([string]$Path) {
    $script:Downloads++
    if ($script:DownloadMode -eq 'Throw') { throw 'mock download failure' }
    $bytes=$script:Fixture.Clone()
    if ($script:DownloadMode -eq 'BadHash') { $bytes[0]=$bytes[0] -bxor 1 }
    if ($script:DownloadMode -eq 'BadSize') { $bytes=@($bytes)+@(0) }
    [IO.File]::WriteAllBytes($Path,[byte[]]$bytes)
}
function Get-UVPNPrerequisiteSignature([string]$Path) {
    $script:Signatures+=@($Path)
    $status='Valid';$publisher='WireGuard LLC'
    $msi=[IO.Path]::GetExtension($Path) -eq '.msi'
    if (($msi -and $script:SignatureMode -eq 'BadMsiStatus') -or
        (-not $msi -and $script:SignatureMode -eq 'BadExecutable')) { $status='NotSigned' }
    if ($msi -and $script:SignatureMode -eq 'BadMsiPublisher') { $publisher='Other Publisher' }
    $certificate=[pscustomobject]@{Publisher=$publisher}
    $certificate | Add-Member -MemberType ScriptMethod -Name GetNameInfo -Value {param($Kind,$Issuer);return $this.Publisher}
    return [pscustomobject]@{Status=$status;SignerCertificate=$certificate}
}
function Start-Process {
    param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$Wait,[switch]$PassThru,$ErrorAction)
    if ($script:ProcessThrows) { throw 'sensitive native error should not be forwarded' }
    $script:Processes+=@([pscustomobject]@{FilePath=$FilePath;Arguments=@($ArgumentList);Verb=$Verb;
        WindowStyle=$WindowStyle;Wait=[bool]$Wait;PassThru=[bool]$PassThru})
    if ([IO.Path]::GetFileName($FilePath) -eq 'powershell.exe') {
        $encoded=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($ArgumentList[-1]))
        $tokens=$null;$parseErrors=$null
        $null=[Management.Automation.Language.Parser]::ParseInput($encoded,[ref]$tokens,[ref]$parseErrors)
        Assert-True ($parseErrors.Count -eq 0) 'elevated command parses'
        if ($script:ChangeDuringUac) {
            $changed=$script:Fixture.Clone();$changed[0]=$changed[0] -bxor 1
            [IO.File]::WriteAllBytes($script:InstallSource,$changed)
        }
        $oldAdmin=$script:Admin;$script:Admin=$true
        try { return [pscustomobject]@{ExitCode=(& $script:RealElevated $script:InstallSource)} }
        catch { return [pscustomobject]@{ExitCode=1} }
        finally { $script:Admin=$oldAdmin }
    }
    if ([IO.Path]::GetFileName($FilePath) -eq 'msiexec.exe') {
        $package=([string]$ArgumentList[1]).Trim('"')
        $exclusive=[IO.File]::Open($package,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $exclusive.Dispose()
    }
    return [pscustomobject]@{ExitCode=$script:InstallerCode}
}
function Invoke-UVPNPrerequisiteInstall([string]$Path) {
    # MSI must be able to open exclusively; a held validation lock is a regression.
    $exclusive=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $exclusive.Dispose()
    $script:InstallSource=$Path
    return & $script:RealInstall $Path
}

try {
    Test-Case 'dot-source has no install or download side effects' {
        Assert-True ($script:ImportSideEffects -eq 0) 'import must not inspect services or start installation'
    }
    Test-Case 'real hardware architecture query works on this PowerShell version' {
        $architecture=& $script:RealArchitecture
        Assert-True ($architecture -in @('X64','Arm64','X86','Arm')) 'native hardware architecture, not process pointer size'
    }
    Test-Case 'valid existing installation is reused without MSI' {
        Reset-Fixture @('Existing')
        $result=Ensure-UVPNWireGuard
        Assert-True ($result.Status -eq 'Existing' -and $result.Ready -and $result.Reused) 'existing result'
        Assert-True ($script:Downloads -eq 0 -and $script:Processes.Count -eq 0 -and $script:Signatures.Count -eq 2) 'no network or MSI'
    }
    Test-Case 'invalid existing executable is never repaired or upgraded' {
        Reset-Fixture @('Existing');$script:SignatureMode='BadExecutable'
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'signature'
        Assert-True ($script:Downloads -eq 0 -and $script:Processes.Count -eq 0) 'no fallback install'
    }
    foreach ($kind in @('Partial','Directory','Service','Registered','Custom')) {
        Test-Case ('existing '+$kind+' prevents installation') {
            Reset-Fixture @($kind)
            $null=Assert-Fails { Ensure-UVPNWireGuard } 'no repair'
            Assert-True ($script:Downloads -eq 0 -and $script:Processes.Count -eq 0) 'no install or tunnel mutation'
        }
    }
    Test-Case 'unsupported missing-install architecture fails before downloading' {
        Reset-Fixture;$script:Architecture='Arm64'
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'Windows x64'
        Assert-True ($script:Downloads -eq 0) 'no incorrect architecture package'
    }
    Test-Case 'new installation uses real installer wrapper and expected arguments' {
        Reset-Fixture
        $result=Ensure-UVPNWireGuard
        Assert-True ($result.Status -eq 'Installed' -and $result.Ready -and -not $result.Reused) 'installed result'
        Assert-True ($script:Downloads -eq 1 -and $script:Processes.Count -eq 2) 'one elevation and one installer'
        $elevation=$script:Processes[0]
        Assert-True ($elevation.Verb -eq 'RunAs' -and $elevation.WindowStyle -eq 'Hidden') 'explicit UAC'
        $process=$script:Processes[1]
        Assert-True ([IO.Path]::GetFileName($process.FilePath) -eq 'msiexec.exe') 'system installer'
        Assert-True (($process.Arguments -join '|') -eq ('/i|"'+$script:ProtectedPath+'"|/qn|/norestart|DO_NOT_LAUNCH=1')) 'fixed quiet arguments'
        Assert-True (-not $process.Verb -and $process.WindowStyle -eq 'Hidden' -and $process.Wait -and $process.PassThru) 'already elevated MSI runs directly'
        Assert-True ($script:ProtectedPath -ne $script:InstallSource) 'separate elevated package copy'
        Assert-True (-not (Test-Path -LiteralPath $script:Temporary.Path)) 'only this temporary directory cleaned'
    }
    Test-Case 'already elevated caller runs directly without UAC' {
        Reset-Fixture;$script:Admin=$true
        $null=Ensure-UVPNWireGuard
        Assert-True ($script:Processes.Count -eq 1 -and -not $script:Processes[0].Verb -and
            [IO.Path]::GetFileName($script:Processes[0].FilePath) -eq 'msiexec.exe') 'direct MSI without RunAs'
    }
    foreach ($mode in @('BadHash','BadSize')) {
        Test-Case ($mode+' package never reaches installer') {
            Reset-Fixture;$script:DownloadMode=$mode
            $null=Assert-Fails { Ensure-UVPNWireGuard } 'verification failed'
            Assert-True ($script:Processes.Count -eq 0) 'package rejected'
            Assert-True (-not (Test-Path -LiteralPath $script:Temporary.Path)) 'temporary cleanup'
        }
    }
    foreach ($mode in @('BadMsiStatus','BadMsiPublisher')) {
        Test-Case ($mode+' prevents installation') {
            Reset-Fixture;$script:SignatureMode=$mode
            $null=Assert-Fails { Ensure-UVPNWireGuard } 'signature'
            Assert-True ($script:Processes.Count -eq 0) 'signer must be valid and expected'
        }
    }
    Test-Case 'installation appearing during download is reused instead of reinstalled' {
        Reset-Fixture @('Absent','Existing')
        $result=Ensure-UVPNWireGuard
        Assert-True ($result.Reused -and $script:Processes.Count -eq 0) 'fresh inventory prevents reinstall'
    }
    Test-Case 'partial installation appearing during download stops' {
        Reset-Fixture @('Absent','Service')
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'no repair'
        Assert-True ($script:Processes.Count -eq 0) 'no concurrent service upgrade'
    }
    Test-Case '3010 stops connection and reports restart required' {
        Reset-Fixture;$script:InstallerCode=3010
        $failure=Assert-Fails { Ensure-UVPNWireGuard } 'must be restarted'
        Assert-True ($failure.Exception.Data['UVPNRestartRequired'] -eq $true) 'typed restart metadata'
        Assert-True ($script:StateIndex -eq 4 -and $script:Processes.Count -eq 2) 'no ready state or repeated installer'
        Assert-True (-not (Test-Path -LiteralPath $script:Temporary.Path)) 'cleanup after restart-required'
    }
    Test-Case 'failed MSI exits without automatic retry' {
        Reset-Fixture;$script:InstallerCode=1603
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'installation failed'
        Assert-True ($script:Processes.Count -eq 2) 'one elevation and one MSI attempt'
    }
    Test-Case 'missing executables after success cannot report ready' {
        Reset-Fixture @('Absent','Absent','Absent','Absent','Partial')
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'both expected'
    }
    Test-Case 'post-install executable signature is checked' {
        Reset-Fixture;$script:SignatureMode='BadExecutable'
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'signature'
        Assert-True ($script:Processes.Count -eq 2) 'post-install verification ran'
    }
    Test-Case 'UAC cancellation is sanitized and not retried' {
        Reset-Fixture;$script:ProcessThrows=$true
        $failure=Assert-Fails { Ensure-UVPNWireGuard } 'cancelled'
        Assert-True ($failure.Exception.Message -notmatch 'sensitive') 'native exception text hidden'
        Assert-True (-not (Test-Path -LiteralPath $script:Temporary.Path)) 'cleanup after cancellation'
    }
    Test-Case 'source changed during UAC never reaches elevated MSI' {
        Reset-Fixture;$script:ChangeDuringUac=$true
        $null=Assert-Fails { Ensure-UVPNWireGuard } 'installation failed'
        Assert-True ($script:Processes.Count -eq 1 -and -not $script:ProtectedPath) 'elevated source revalidation blocks changed bytes'
    }
    Test-Case 'installation appearing during UAC is verified and reused' {
        Reset-Fixture @('Absent','Absent','Existing')
        $result=Ensure-UVPNWireGuard
        Assert-True ($result.Reused -and $script:Processes.Count -eq 1) 'no MSI reconfiguration'
    }
    Test-Case 'protected staging ACL has Admin owner and only Admin SYSTEM grants' {
        foreach ($directory in @($false,$true)) {
            $acl=New-UVPNPrerequisiteAdminSecurity -Directory:$directory
            Assert-True ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-32-544') 'Admin owns protected staging'
            $rules=@($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
            Assert-True ($rules.Count -eq 2 -and @($rules | Where-Object { $_.IdentityReference.Value -notin @('S-1-5-32-544','S-1-5-18') }).Count -eq 0) 'no current-user write grant'
            Assert-True $acl.AreAccessRulesProtected 'no inherited grants'
        }
    }
    Test-Case 'temporary cleanup refuses an unrelated directory' {
        $unrelated=Join-Path $script:TestRoot 'unrelated'
        $null=New-Item -ItemType Directory -Path $unrelated
        $file=Join-Path $unrelated $script:UVPNPrerequisiteMsiName
        [IO.File]::WriteAllBytes($file,$script:Fixture)
        Remove-UVPNPrerequisiteTemp ([pscustomobject]@{Parent=$script:TestRoot;Path=$unrelated;Name='unrelated'})
        Assert-True (Test-Path -LiteralPath $file) 'unrelated content remains'
    }
    Write-Host ('Passed '+$script:Passed+' prerequisite checks; all downloads, signatures and installers were mocked.')
} finally {
    # This suite created this exact directory; never recurse on a computed unknown path.
    $resolved=[IO.Path]::GetFullPath($script:TestRoot)
    if ([IO.Path]::GetDirectoryName($resolved).TrimEnd('\','/') -ieq $script:TestParent -and
        [IO.Path]::GetFileName($resolved) -cmatch '^univpn-prereq-tests-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
