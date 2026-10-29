<#
.SYNOPSIS
Reuse a verified WireGuard installation, or install the pinned MSI only when absent.
.NOTES
Dot-sourcing defines functions only. Ensure-UVPNWireGuard is called explicitly by
the Windows client, never by the Linux container. An absent installation needs
UAC; creating the later tunnel may require another confirmation.
#>

$script:UVPNPrerequisiteMsiUrl = 'https://download.wireguard.com/windows-client/wireguard-amd64-1.1.1.msi'
$script:UVPNPrerequisiteMsiSize = 3293184
$script:UVPNPrerequisiteMsiSha256 = '7bfed60ad61b785c914b38b61555a975488e1d3ec472dbfb2fcdf498fca75242'
$script:UVPNPrerequisiteMsiName = 'wireguard-amd64-1.1.1.msi'

function Get-UVPNPrerequisiteArchitecture {
    try {
        $values = @(Get-CimInstance -ClassName Win32_Processor -Property Architecture -ErrorAction Stop |
            Select-Object -ExpandProperty Architecture -Unique)
        if ($values.Count -ne 1) { throw 'Ambiguous processor architecture.' }
        switch ([int]$values[0]) {
            9 { return 'X64' }
            12 { return 'Arm64' }
            0 { return 'X86' }
            5 { return 'Arm' }
            default { throw 'Unsupported processor architecture.' }
        }
    } catch { throw 'Cannot determine Windows hardware architecture; no package was selected.' }
}

function Get-UVPNPrerequisiteState {
    $base = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'WireGuard'
    $wireguard = Join-Path $base 'wireguard.exe'
    $wg = Join-Path $base 'wg.exe'
    try {
        $services = @(Get-Service -Name 'WireGuard*' -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -like 'NoServiceFoundForGivenName,*') { $services = @() }
        else { throw 'Cannot inspect existing WireGuard services; no installation was attempted.' }
    }
    $registered = $false
    foreach ($registry in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')) {
        if (Test-Path -LiteralPath $registry) {
            $items = @(Get-ItemProperty -Path ($registry + '\*') -ErrorAction Stop |
                Where-Object { $_.DisplayName -eq 'WireGuard' })
            if ($items.Count) { $registered = $true }
        }
    }
    $custom = $false
    foreach ($name in @('wireguard.exe','wg.exe')) {
        $commands = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
        foreach ($command in $commands) {
            if ([IO.Path]::GetFullPath($command.Source) -ine [IO.Path]::GetFullPath((Join-Path $base $name))) {
                $custom = $true
            }
        }
    }
    return [pscustomobject]@{
        WireGuardExe=$wireguard; WgExe=$wg
        WireGuardExists=(Test-Path -LiteralPath $wireguard -PathType Leaf)
        WgExists=(Test-Path -LiteralPath $wg -PathType Leaf)
        DirectoryExists=(Test-Path -LiteralPath $base)
        ServicesExist=($services.Count -gt 0); Registered=$registered; CustomPathExists=$custom
    }
}

function Get-UVPNPrerequisiteSignature([string]$Path) {
    return Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
}

function Assert-UVPNPrerequisiteSignature([string]$Path) {
    try {
        $signature = Get-UVPNPrerequisiteSignature $Path
        if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
            $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -cne 'WireGuard LLC') {
            throw 'Invalid signer.'
        }
    } catch { throw 'WireGuard file signature verification failed. A valid WireGuard LLC signature is required.' }
}

function New-UVPNPrerequisiteTemp {
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\','/')
    $leaf = 'univpn-wg-' + [Guid]::NewGuid().ToString('N')
    $directory = Join-Path $parent $leaf
    $null = New-Item -ItemType Directory -Path $directory -ErrorAction Stop
    try {
        $acl = Get-Acl -LiteralPath $directory -ErrorAction Stop
        $acl.SetAccessRuleProtection($true,$false)
        foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
                           'S-1-5-18','S-1-5-32-544')) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl',
                'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $directory -AclObject $acl -ErrorAction Stop
        return [pscustomobject]@{Parent=$parent;Path=$directory;Name=$leaf}
    } catch {
        [IO.Directory]::Delete($directory,$false)
        throw 'Cannot restrict the temporary WireGuard package directory.'
    }
}

function Remove-UVPNPrerequisiteTemp($Temporary) {
    if (-not $Temporary) { return }
    try {
        $parent = [IO.Path]::GetFullPath([string]$Temporary.Parent).TrimEnd('\','/')
        $directory = [IO.Path]::GetFullPath([string]$Temporary.Path)
        $name = [IO.Path]::GetFileName($directory)
        if ($name -cnotmatch '^univpn-wg-[0-9a-f]{32}$' -or $name -cne $Temporary.Name -or
            [IO.Path]::GetDirectoryName($directory).TrimEnd('\','/') -ine $parent) { return }
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { return }
        $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { return }
        $package = Join-Path $directory $script:UVPNPrerequisiteMsiName
        if (Test-Path -LiteralPath $package -PathType Leaf) {
            Remove-Item -LiteralPath $package -Force -ErrorAction Stop
        }
        # Nonrecursive deletion never walks unexpected files or linked trees.
        [IO.Directory]::Delete($directory,$false)
    } catch { Write-Verbose 'The temporary WireGuard package could not be fully removed.' }
}

function Get-UVPNPrerequisitePackage([string]$Path) {
    $response = $null; $inputStream = $null; $output = $null
    try {
        $request = [Net.HttpWebRequest]::Create($script:UVPNPrerequisiteMsiUrl)
        $request.AllowAutoRedirect = $false
        $request.Timeout = 15000; $request.ReadWriteTimeout = 15000
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -ne $script:UVPNPrerequisiteMsiSize) {
            throw 'Unexpected response.'
        }
        $inputStream = $response.GetResponseStream()
        $output = [IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $buffer = New-Object byte[] 65536
        $total = 0; $clock = [Diagnostics.Stopwatch]::StartNew()
        while (($count = $inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
            $total += $count
            if ($total -gt $script:UVPNPrerequisiteMsiSize -or $clock.Elapsed.TotalSeconds -gt 90) { throw 'Download limit.' }
            $output.Write($buffer,0,$count)
        }
        if ($total -ne $script:UVPNPrerequisiteMsiSize) { throw 'Incomplete package.' }
    } catch { throw 'Cannot download the pinned official WireGuard package; no installation was attempted.' }
    finally {
        if ($output) { $output.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Invoke-UVPNPrerequisiteMsi([string]$Path) {
    $installer = Join-Path ([Environment]::GetFolderPath('System')) 'msiexec.exe'
    $arguments = @('/i',('"' + $Path + '"'),'/qn','/norestart','DO_NOT_LAUNCH=1')
    try {
        $options = @{FilePath=$installer;ArgumentList=$arguments;WindowStyle='Hidden';
            Wait=$true;PassThru=$true;ErrorAction='Stop'}
        $process = Start-Process @options
        return [int]$process.ExitCode
    } catch { throw 'WireGuard installation was cancelled or could not start; no automatic retry.' }
}

function Assert-UVPNPrerequisitePackage([string]$Path) {
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        if ($stream.Length -ne $script:UVPNPrerequisiteMsiSize) { throw 'Unexpected package size.' }
        $digest = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        if ($digest -cne $script:UVPNPrerequisiteMsiSha256) {
            throw 'Invalid package.'
        }
        Assert-UVPNPrerequisiteSignature $Path
    } catch { throw 'WireGuard package hash, size or signature verification failed.' }
    finally { if ($stream) { $stream.Dispose() } }
}

function New-UVPNPrerequisiteAdminSecurity([switch]$Directory) {
    $acl = if ($Directory) { [Security.AccessControl.DirectorySecurity]::new() }
           else { [Security.AccessControl.FileSecurity]::new() }
    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl.SetOwner($administrators)
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @('S-1-5-32-544','S-1-5-18')) {
        $identity = [Security.Principal.SecurityIdentifier]::new($sid)
        $rule = if ($Directory) {
            [Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
        } else { [Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow') }
        $acl.AddAccessRule($rule)
    }
    return $acl
}

function New-UVPNPrerequisiteAdminTemp {
    $parent = Join-Path ([Environment]::GetFolderPath('Windows')) 'Temp'
    $name = 'univpn-wg-' + [Guid]::NewGuid().ToString('N')
    $path = Join-Path $parent $name
    if (Test-Path -LiteralPath $path) { throw 'Temporary path already exists.' }
    $directory = [IO.DirectoryInfo]::new($path)
    $acl = New-UVPNPrerequisiteAdminSecurity -Directory
    if ($PSVersionTable.PSEdition -eq 'Core') { [IO.FileSystemAclExtensions]::Create($directory,$acl) }
    else { $directory.Create($acl) }
    return [pscustomobject]@{Parent=$parent;Path=$path;Name=$name}
}

function New-UVPNPrerequisiteAdminFile([string]$Path) {
    $acl = New-UVPNPrerequisiteAdminSecurity
    # CreateNew atomically applies Administrator ownership and the private DACL.
    $rights = [Security.AccessControl.FileSystemRights]::FullControl
    if ($PSVersionTable.PSEdition -eq 'Core') {
        return [IO.FileSystemAclExtensions]::Create([IO.FileInfo]::new($Path),[IO.FileMode]::CreateNew,
            $rights,[IO.FileShare]::None,65536,[IO.FileOptions]::None,$acl)
    }
    return [IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,$rights,[IO.FileShare]::None,
        65536,[IO.FileOptions]::None,$acl)
}

function Copy-UVPNPrerequisiteAdminPackage([string]$Source,[string]$Destination) {
    $inputStream = $null; $output = $null
    try {
        $inputStream = [IO.File]::Open($Source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        # Revalidate after elevation; the user's source could change during UAC.
        Assert-UVPNPrerequisitePackage $Source
        $output = New-UVPNPrerequisiteAdminFile $Destination
        $inputStream.CopyTo($output)
        $output.Flush($true)
    } finally {
        if ($output) { $output.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
    }
    Assert-UVPNPrerequisitePackage $Destination
}

function Invoke-UVPNPrerequisiteElevatedInstall([string]$Source) {
    if (-not (Test-UVPNPrerequisiteAdministrator)) { throw 'Elevated prerequisite stage requires administrator authorization.' }
    $existing = Resolve-UVPNPrerequisiteState (Get-UVPNPrerequisiteState)
    if ($existing) { return 10 }
    $temporary = $null
    try {
        $temporary = New-UVPNPrerequisiteAdminTemp
        $package = Join-Path $temporary.Path $script:UVPNPrerequisiteMsiName
        Copy-UVPNPrerequisiteAdminPackage $Source $package
        $existing = Resolve-UVPNPrerequisiteState (Get-UVPNPrerequisiteState)
        if ($existing) { return 10 }
        # Only Admin/SYSTEM can modify this copy; no file handles remain open.
        return Invoke-UVPNPrerequisiteMsi $package
    } finally { Remove-UVPNPrerequisiteTemp $temporary }
}

function Invoke-UVPNPrerequisiteInstall([string]$Path) {
    try {
        if (Test-UVPNPrerequisiteAdministrator) { return Invoke-UVPNPrerequisiteElevatedInstall $Path }
        $source = "'" + $Path.Replace("'","''") + "'"
        $pins = '$script:UVPNPrerequisiteMsiName=''' + $script:UVPNPrerequisiteMsiName + "'`n" +
            '$script:UVPNPrerequisiteMsiSize=' + $script:UVPNPrerequisiteMsiSize + "`n" +
            '$script:UVPNPrerequisiteMsiSha256=''' + $script:UVPNPrerequisiteMsiSha256 + "'`n"
        # Definitions were captured at module load, not reread from a user-writable
        # script while UAC is pending. The only dynamic input is the public MSI path.
        $body = $pins + $script:UVPNPrerequisiteElevatedDefinitions + "`ntry { exit (Invoke-UVPNPrerequisiteElevatedInstall $source) } catch { exit 1 }"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
        $shell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
        $process = Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded) `
            -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
        return [int]$process.ExitCode
    } catch { throw 'WireGuard installation was cancelled or failed verification; no automatic retry.' }
}

function Test-UVPNPrerequisiteAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Enter-UVPNPrerequisiteGuard {
    $mutex = $null
    try {
        $mutex = [Threading.Mutex]::new($false,'Global\UniVPNWireGuardPrerequisites')
        try { $acquired = $mutex.WaitOne(30000) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'Another setup is active.' }
        return $mutex
    } catch {
        if ($mutex) { $mutex.Dispose() }
        throw 'Another WireGuard setup is active or cannot be safely inspected; retry after it finishes.'
    }
}

function Exit-UVPNPrerequisiteGuard($Mutex) {
    try { $Mutex.ReleaseMutex() }
    finally { $Mutex.Dispose() }
}

function New-UVPNPrerequisiteResult($State,[bool]$Reused) {
    return [pscustomobject]@{Status=if ($Reused) {'Existing'} else {'Installed'};
        Ready=$true;Reused=$Reused;RestartRequired=$false;
        WireGuardExe=$State.WireGuardExe;WgExe=$State.WgExe}
}

function Resolve-UVPNPrerequisiteState($State) {
    if ($State.WireGuardExists -and $State.WgExists) {
        Assert-UVPNPrerequisiteSignature $State.WireGuardExe
        Assert-UVPNPrerequisiteSignature $State.WgExe
        return New-UVPNPrerequisiteResult $State $true
    }
    if ($State.WireGuardExists -or $State.WgExists -or $State.DirectoryExists -or
        $State.ServicesExist -or $State.Registered -or $State.CustomPathExists) {
        throw 'Existing, partial or custom WireGuard installation detected. Inspect it manually; no repair, upgrade or tunnel stop was attempted.'
    }
    return $null
}

function Initialize-UVPNWireGuard {
    $state = Get-UVPNPrerequisiteState
    $existing = Resolve-UVPNPrerequisiteState $state
    if ($existing) { return $existing }
    if ((Get-UVPNPrerequisiteArchitecture) -cne 'X64') {
        throw 'Automatic WireGuard installation currently supports Windows x64 only; use an official installation for this architecture.'
    }
    $temporary = $null; $lock = $null
    try {
        $temporary = New-UVPNPrerequisiteTemp
        $package = Join-Path $temporary.Path $script:UVPNPrerequisiteMsiName
        Get-UVPNPrerequisitePackage $package
        # Lock while validating. MSI requires exclusive access during installation.
        $lock = [IO.File]::Open($package,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        if ($lock.Length -ne $script:UVPNPrerequisiteMsiSize) {
            throw 'WireGuard package size verification failed; no installation was attempted.'
        }
        $digest = (Get-FileHash -LiteralPath $package -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        if ($digest -cne $script:UVPNPrerequisiteMsiSha256) {
            throw 'WireGuard package SHA256 or size verification failed; no installation was attempted.'
        }
        Assert-UVPNPrerequisiteSignature $package
        # Another installer outside this client's mutex may have appeared while downloading.
        $existing = Resolve-UVPNPrerequisiteState (Get-UVPNPrerequisiteState)
        if ($existing) { return $existing }
        $lock.Dispose(); $lock = $null
        $exitCode = Invoke-UVPNPrerequisiteInstall $package
        if ($exitCode -eq 10) {
            $existing = Resolve-UVPNPrerequisiteState (Get-UVPNPrerequisiteState)
            if ($existing) { return $existing }
            throw 'The concurrently installed WireGuard could not be verified.'
        }
        if ($exitCode -eq 3010) {
            $error = [InvalidOperationException]::new('WireGuard installation completed but Windows must be restarted. Restart before connecting; do not repeat installation.')
            $error.Data['UVPNRestartRequired'] = $true
            throw $error
        }
        if ($exitCode -ne 0) { throw 'WireGuard installation failed; no automatic retry or changes to existing tunnels were requested.' }
        $state = Get-UVPNPrerequisiteState
        if (-not $state.WireGuardExists -or -not $state.WgExists) { throw 'WireGuard installation did not produce both expected executables.' }
        Assert-UVPNPrerequisiteSignature $state.WireGuardExe
        Assert-UVPNPrerequisiteSignature $state.WgExe
        return New-UVPNPrerequisiteResult $state $false
    } finally {
        if ($lock) { $lock.Dispose() }
        Remove-UVPNPrerequisiteTemp $temporary
    }
}

function Ensure-UVPNWireGuard {
    $guard = Enter-UVPNPrerequisiteGuard
    try { return Initialize-UVPNWireGuard }
    finally { Exit-UVPNPrerequisiteGuard $guard }
}

# Capture only this implementation's elevated stage before any caller can mock
# or replace a function; this does not inspect services, download, or install.
$script:UVPNPrerequisiteElevatedDefinitions = (@(
    'Get-UVPNPrerequisiteState','Get-UVPNPrerequisiteSignature','Assert-UVPNPrerequisiteSignature',
    'Assert-UVPNPrerequisitePackage','Test-UVPNPrerequisiteAdministrator','New-UVPNPrerequisiteResult',
    'Resolve-UVPNPrerequisiteState','New-UVPNPrerequisiteAdminSecurity','New-UVPNPrerequisiteAdminTemp',
    'New-UVPNPrerequisiteAdminFile','Copy-UVPNPrerequisiteAdminPackage','Invoke-UVPNPrerequisiteMsi',
    'Invoke-UVPNPrerequisiteElevatedInstall','Remove-UVPNPrerequisiteTemp'
) | ForEach-Object { 'function ' + $_ + " {`n" + (Get-Item ('Function:\'+$_)).ScriptBlock.ToString() + "`n}" }) -join "`n"
