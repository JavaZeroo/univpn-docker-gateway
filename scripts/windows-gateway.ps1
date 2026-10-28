<#
.SYNOPSIS
Read-only plan, or explicitly install/remove one dedicated UniVPN WG tunnel.
.EXAMPLE
./scripts/windows-gateway.ps1 -TunnelConfig ./private/univpn-lab.conf -GatewayConfig ./private/vpn.json -Endpoint 127.0.0.1:51829
.EXAMPLE
./scripts/windows-gateway.ps1 -Mode Install -TunnelConfig ./private/univpn-lab.conf -GatewayConfig ./private/vpn.json -Endpoint 127.0.0.1:51829 -Elevate -ResultPath ./private/windows-result.json
.EXAMPLE
./scripts/windows-gateway.ps1 -Mode Uninstall -TunnelConfig ./private/univpn-lab.conf -Elevate
.NOTES
Only univpn-*.conf basenames are accepted. No existing tunnel is stopped or
replaced. Configs need one Interface/Peer and an explicit MTU of 1280..1420.
Elevation is opt-in and still requires Windows UAC approval. Only paths and
nonsecret validation options enter the child command; keys stay in the file.
The elevated shell uses process-only ExecutionPolicy Bypass so Windows
PowerShell 5.1 can load this local script. No persistent policy is changed.
Install checks for a newly created WireGuard adapter, sets only its IPv4
ActiveStore metric to zero, and verifies target routes. This metric setting
is not promised to survive tunnel service/adapter recreation.
Official service commands: https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md
#>
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Uninstall')][string]$Mode = 'Plan',
    [string]$TunnelConfig,
    [string]$GatewayConfig,
    [string]$Endpoint,
    [string]$WireGuardExe,
    [switch]$Elevate,
    [string]$ResultPath
)

$script:UVPNGatewayScriptPath = $PSCommandPath

function Resolve-UVPNFilePath([string]$Value, [switch]$MustExist) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00-\x1f"]') {
        throw 'A valid local file path is required.'
    }
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Value)
    if ($full.StartsWith('\\') -or -not [IO.Path]::IsPathRooted($full)) {
        throw 'Use a local filesystem path, not a network share.'
    }
    $full = [IO.Path]::GetFullPath($full)
    if ($MustExist -and -not [IO.File]::Exists($full)) { throw 'A required local file does not exist.' }
    return $full
}

function Get-UVPNTunnelName([string]$Path) {
    if ([IO.Path]::GetExtension($Path) -cne '.conf') { throw 'The tunnel must use a .conf file.' }
    $name = [IO.Path]::GetFileNameWithoutExtension($Path)
    if ($name -cnotmatch '^univpn-[A-Za-z0-9][A-Za-z0-9_-]{0,24}$') {
        throw 'Only a dedicated univpn- tunnel basename of at most 32 characters is allowed.'
    }
    return $name
}

function ConvertTo-UVPNCidr([string]$Value, [switch]$Network) {
    if ($Value -cnotmatch '^((?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3})/(0|[1-9][0-9]?)$') {
        throw 'Only canonical IPv4 CIDRs are supported.'
    }
    $address = $Matches[1]
    $prefix = [int]$Matches[2]
    $octets = @($address.Split('.') | ForEach-Object { [int]$_ })
    if ($prefix -gt 32 -or @($octets | Where-Object { $_ -gt 255 }).Count) {
        throw 'Invalid IPv4 CIDR.'
    }
    $number = [uint64]$octets[0] * 16777216 + [uint64]$octets[1] * 65536 +
        [uint64]$octets[2] * 256 + [uint64]$octets[3]
    $size = [uint64][math]::Pow(2, 32 - $prefix)
    if ($Network -and ($number % $size) -ne 0) { throw 'Destination CIDRs must use their network address.' }
    if ($prefix -eq 0) { throw 'Default routes are not allowed.' }
    return [pscustomobject]@{ Text=$Value; Address=$address; Prefix=$prefix; Start=$number; End=($number + $size - 1) }
}

function Assert-UVPNKey([string]$Value) {
    if ($Value -cnotmatch '^[A-Za-z0-9+/]{43}=$') { throw 'A WireGuard key is missing, malformed, or a placeholder.' }
    $bytes = $null
    try {
        $bytes = [Convert]::FromBase64String($Value)
        if ($bytes.Length -ne 32 -or [Convert]::ToBase64String($bytes) -cne $Value -or
            @($bytes | Where-Object { $_ -ne 0 }).Count -eq 0) {
            throw 'Invalid key.'
        }
    } catch { throw 'A WireGuard key is missing, malformed, or a placeholder.' }
    finally { if ($null -ne $bytes) { [Array]::Clear($bytes, 0, $bytes.Length) } }
}

function Read-UVPNValidatedConfig([string]$Text, [string]$GatewayPath, [string]$ExpectedEndpoint) {
    if ($ExpectedEndpoint -cnotmatch '^127\.0\.0\.1:([1-9][0-9]{0,4})$' -or [int]$Matches[1] -gt 65535) {
        throw 'An explicit 127.0.0.1:UDP-port endpoint is required.'
    }
    $sections = @{}
    $current = $null
    foreach ($line in ($Text -split '\r?\n')) {
        $item = $line.Trim()
        if (-not $item -or $item.StartsWith('#') -or $item.StartsWith(';')) { continue }
        if ($item -match '^\[([A-Za-z]+)\]$') {
            $current = $Matches[1]
            if ($current -notin @('Interface', 'Peer') -or $sections.ContainsKey($current)) {
                throw 'Exactly one Interface and one Peer are required.'
            }
            $sections[$current] = @{}
            continue
        }
        if (-not $current -or $item -notmatch '^([A-Za-z][A-Za-z0-9]*)\s*=\s*(.+)$') {
            throw 'Unsupported WireGuard configuration syntax.'
        }
        $key, $value = $Matches[1], $Matches[2].Trim()
        $allowed = if ($current -eq 'Interface') { @('Address','PrivateKey','MTU') }
                   else { @('PublicKey','AllowedIPs','Endpoint','PersistentKeepalive') }
        if ($key -notin $allowed -or $sections[$current].ContainsKey($key)) {
            throw 'Unknown or duplicate option; DNS, hooks, and extra routing options are forbidden.'
        }
        $sections[$current][$key] = $value
    }
    if ($sections.Count -ne 2 -or -not $sections.ContainsKey('Interface') -or -not $sections.ContainsKey('Peer')) {
        throw 'Exactly one Interface and one Peer are required.'
    }
    $iface, $peer = $sections['Interface'], $sections['Peer']
    Assert-UVPNKey $iface['PrivateKey']
    Assert-UVPNKey $peer['PublicKey']
    if ($iface['PrivateKey'] -ceq $peer['PublicKey']) { throw 'Private and peer public key fields must differ.' }
    $null = $iface.Remove('PrivateKey')
    $null = $peer.Remove('PublicKey')
    if ($iface['MTU'] -notmatch '^1[0-9]{3}$' -or [int]$iface['MTU'] -lt 1280 -or [int]$iface['MTU'] -gt 1420) {
        throw 'Specify an explicit MTU between 1280 and 1420 for this gateway experiment.'
    }
    if ($peer['Endpoint'] -cne $ExpectedEndpoint) { throw 'The peer endpoint differs from the explicit loopback endpoint.' }
    if ($peer.ContainsKey('PersistentKeepalive') -and
        ($peer['PersistentKeepalive'] -notmatch '^[0-9]{1,5}$' -or [int]$peer['PersistentKeepalive'] -gt 65535)) {
        throw 'Invalid PersistentKeepalive value.'
    }
    try { $gateway = [IO.File]::ReadAllText($GatewayPath) | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'GatewayConfig could not be read as JSON.' }
    if (-not $gateway -or -not $gateway.PSObject.Properties['allowed_cidrs'] -or
        -not $gateway.PSObject.Properties['wireguard_client'] -or $gateway.allowed_cidrs -is [string]) {
        throw 'GatewayConfig requires allowed_cidrs and wireguard_client.'
    }
    $client = ConvertTo-UVPNCidr ([string]$gateway.wireguard_client)
    $address = ConvertTo-UVPNCidr $iface['Address']
    if ($client.Prefix -ne 32 -or $address.Text -cne $client.Text -or
        $client.Start -eq 0 -or $client.Address.StartsWith('127.') -or $client.Start -ge 3758096384) {
        throw 'Interface Address must equal GatewayConfig wireguard_client as one usable IPv4 /32.'
    }
    $expected = @($gateway.allowed_cidrs | ForEach-Object { ConvertTo-UVPNCidr ([string]$_) -Network })
    $actual = @(([string]$peer['AllowedIPs']).Split(',') | ForEach-Object { ConvertTo-UVPNCidr $_.Trim() -Network })
    if (-not $expected.Count -or -not $actual.Count -or
        @($expected.Text | Sort-Object -Unique).Count -ne $expected.Count -or
        @($actual.Text | Sort-Object -Unique).Count -ne $actual.Count -or
        @(Compare-Object @($expected.Text | Sort-Object) @($actual.Text | Sort-Object)).Count) {
        throw 'Peer AllowedIPs must exactly match GatewayConfig allowed_cidrs without duplicates.'
    }
    $covered = [uint64]0
    foreach ($range in ($actual | Sort-Object Start)) {
        if ($range.Start -le $covered) { $covered = [math]::Max($covered, [uint64]($range.End + 1)) }
        if ($client.Start -ge $range.Start -and $client.Start -le $range.End) { throw 'Client address overlaps a destination.' }
    }
    if ($covered -ge 4294967296) { throw 'A collection of destinations must not cover the default route.' }
    return [pscustomobject]@{ ClientAddress=$address.Text; Destinations=@($actual.Text); MTU=[int]$iface['MTU']; Endpoint=$ExpectedEndpoint }
}

function Test-UVPNAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UVPNExistingService([string]$Name) {
    try { return Get-Service -Name $Name -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -like 'NoServiceFoundForGivenName,*') { return $null }
        throw 'Cannot inspect the dedicated tunnel service; refusing to change it.'
    }
}

function Get-UVPNAdapters {
    # Keep identities for pre-existing adapters internally; output only this
    # tunnel's presence. A renamed old interface must never become our target.
    return @(Get-NetAdapter -IncludeHidden -ErrorAction Stop |
        Select-Object Name,InterfaceDescription,ifIndex,InterfaceGuid,Status)
}

function Get-UVPNClockMilliseconds {
    return ([Diagnostics.Stopwatch]::GetTimestamp() * 1000.0 / [Diagnostics.Stopwatch]::Frequency)
}

function Wait-UVPNNewAdapter([string]$Name, [string]$ServiceName, [string[]]$PreviousIdentities) {
    $deadline = (Get-UVPNClockMilliseconds) + 9000
    do {
        $service = Get-UVPNExistingService $ServiceName
        $matching = @(Get-UVPNAdapters | Where-Object { $_.Name -ceq $Name })
        if ((Get-UVPNClockMilliseconds) -ge $deadline) { break }
        if ($matching.Count -gt 1) { throw 'More than one adapter has the dedicated tunnel name.' }
        if ($matching.Count -eq 1) {
            $adapter = $matching[0]
            if ($adapter.InterfaceDescription -cne 'WireGuard Tunnel') {
                throw 'The dedicated adapter is not a verified WireGuard Tunnel adapter.'
            }
            $identity = [string]$adapter.InterfaceGuid
            $guid = [guid]::Empty
            if (-not [guid]::TryParse($identity, [ref]$guid) -or $guid -eq [guid]::Empty -or
                $identity -in $PreviousIdentities -or [int]$adapter.ifIndex -le 0) {
                throw 'The adapter identity is missing or was already present before installation.'
            }
            if ($service -and $service.Name -ceq $ServiceName -and $service.Status -eq 'Running' -and
                $adapter.Status -eq 'Up') {
                return $adapter
            }
        }
        if ((Get-UVPNClockMilliseconds) -ge $deadline) { break }
        Start-Sleep -Milliseconds 200
    } while ($true)
    throw 'The newly installed service and WireGuard adapter did not become ready within 9 seconds.'
}

function Set-UVPNNewAdapterPriority($Adapter, [string]$Name) {
    # Recheck identity immediately before the only network mutation. Never
    # select another interface by metric, description, or a broad name pattern.
    if ($Name -cnotmatch '^univpn-[A-Za-z0-9][A-Za-z0-9_-]{0,24}$' -or $Adapter.Name -cne $Name) {
        throw 'Only the newly installed dedicated tunnel can receive the gateway priority.'
    }
    $current = @(Get-UVPNAdapters | Where-Object { $_.Name -ceq $Name })
    if ($current.Count -ne 1 -or $current[0].InterfaceDescription -cne 'WireGuard Tunnel' -or
        [string]$current[0].InterfaceGuid -ine [string]$Adapter.InterfaceGuid -or
        [int]$current[0].ifIndex -ne [int]$Adapter.ifIndex -or $current[0].Status -ne 'Up') {
        throw 'The new WireGuard adapter changed before its metric could be set.'
    }
    Set-NetIPInterface -InterfaceIndex ([int]$Adapter.ifIndex) -AddressFamily IPv4 `
        -AutomaticMetric Disabled -InterfaceMetric 0 -PolicyStore ActiveStore -ErrorAction Stop
    $settings = @(Get-NetIPInterface -InterfaceIndex ([int]$Adapter.ifIndex) -AddressFamily IPv4 `
        -PolicyStore ActiveStore -ErrorAction Stop)
    if ($settings.Count -ne 1 -or $settings[0].AutomaticMetric -ne 'Disabled' -or
        [int]$settings[0].InterfaceMetric -ne 0) {
        throw 'The new WireGuard IPv4 interface did not retain the requested ActiveStore metric.'
    }
}

function Get-UVPNOfficialExecutable([string]$Path) {
    if (-not $Path) { $Path = Join-Path $env:ProgramFiles 'WireGuard/wireguard.exe' }
    $resolved = Resolve-UVPNFilePath $Path -MustExist
    if ([IO.Path]::GetFileName($resolved) -ine 'wireguard.exe') { throw 'Use the official wireguard.exe.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved -ErrorAction Stop
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne 'WireGuard LLC') {
        throw 'wireguard.exe must have a valid WireGuard LLC signature.'
    }
    return $resolved
}

function Get-UVPNTargetRoutes([string[]]$Destinations) {
    foreach ($cidr in $Destinations) {
        $target = (ConvertTo-UVPNCidr $cidr -Network).Address
        try {
            $route = @(Find-NetRoute -RemoteIPAddress $target -ErrorAction Stop |
                Where-Object { $_.PSObject.Properties['DestinationPrefix'] }) | Select-Object -First 1
            if (-not $route) { throw 'No route.' }
            [pscustomobject]@{ Target=$cidr; Status='Resolved'; InterfaceAlias=$route.InterfaceAlias;
                InterfaceIndex=$route.InterfaceIndex; DestinationPrefix=$route.DestinationPrefix;
                NextHop=$route.NextHop; RouteMetric=$route.RouteMetric }
        } catch { [pscustomobject]@{ Target=$cidr; Status='Unavailable' } }
    }
}

function Write-UVPNStatus([string]$Path, $Result) {
    if (-not $Path) { return }
    # A result file deliberately omits paths, addresses, routes, keys and errors.
    $status = [ordered]@{ Timestamp=[DateTime]::UtcNow.ToString('o'); Mode=$Result.Mode; Status=$Result.Status }
    $json = ConvertTo-Json -InputObject $status -Compress
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
}

function Invoke-UVPNElevation($Options) {
    $parts = @('&', "'" + $script:UVPNGatewayScriptPath.Replace("'", "''") + "'")
    foreach ($name in @('Mode','TunnelConfig','GatewayConfig','Endpoint','WireGuardExe','ResultPath')) {
        $value = $Options[$name]
        if ($value) { $parts += @('-' + $name, "'" + ([string]$value).Replace("'", "''") + "'") }
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(($parts -join ' ')))
    $shell = (Get-Process -Id $PID).Path
    # This option applies to this child only, never CurrentUser/LocalMachine.
    $child = Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded) `
        -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
    if ($child.ExitCode -ne 0) { throw 'The elevated operation failed or was cancelled; no automatic retry.' }
}

function Invoke-UVPNGateway {
    [CmdletBinding()]
    param([ValidateSet('Plan','Install','Uninstall')][string]$Mode='Plan', [string]$TunnelConfig,
          [string]$GatewayConfig, [string]$Endpoint, [string]$WireGuardExe, [switch]$Elevate, [string]$ResultPath)
    $ErrorActionPreference = 'Stop'
    $tunnelPath = Resolve-UVPNFilePath $TunnelConfig -MustExist:($Mode -ne 'Uninstall')
    $name = Get-UVPNTunnelName $tunnelPath
    $serviceName = 'WireGuardTunnel$' + $name
    $gatewayPath = if ($Mode -ne 'Uninstall') { Resolve-UVPNFilePath $GatewayConfig -MustExist } else { $null }
    $recordPath = if ($ResultPath) { Resolve-UVPNFilePath $ResultPath } else { $null }
    if ($recordPath) {
        if ([IO.Path]::GetExtension($recordPath) -ine '.json' -or
            $recordPath -in @($tunnelPath, $gatewayPath, $script:UVPNGatewayScriptPath) -or
            ((Test-Path -LiteralPath $recordPath) -and (Get-Item -LiteralPath $recordPath).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint))) {
            throw 'ResultPath must be a separate local JSON status file, not an input or link.'
        }
    }
    $stream = $null
    $installationMayExist = $false
    try {
        $validated = $null
        if ($Mode -ne 'Uninstall') {
            # Hold a read-only sharing lock through install, preventing edits or
            # replacement between our whitelist checks and WireGuard's read.
            $stream = [IO.File]::Open($tunnelPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8, $true, 1024, $true)
            try { $validated = Read-UVPNValidatedConfig $reader.ReadToEnd() $gatewayPath $Endpoint }
            finally { $reader.Dispose() }
        }
        $exe = Get-UVPNOfficialExecutable $WireGuardExe
        $admin = Test-UVPNAdministrator
        $existing = Get-UVPNExistingService $serviceName
        $beforeAdapters = @(Get-UVPNAdapters)
        $existingAdapter = @($beforeAdapters | Where-Object { $_.Name -ieq $name })
        $previousIdentities = @($beforeAdapters | ForEach-Object { [string]$_.InterfaceGuid })
        $routes = if ($validated) { @(Get-UVPNTargetRoutes $validated.Destinations) } else { @() }
        $result = [ordered]@{ Mode=$Mode; Status='Plan'; Tunnel=$name; ServiceName=$serviceName;
            Administrator=$admin; ServiceExists=($null -ne $existing); AdapterExists=($existingAdapter.Count -gt 0); Endpoint=$Endpoint;
            ClientAddress=if ($validated) { $validated.ClientAddress } else { $null };
            AllowedIPs=if ($validated) { $validated.Destinations } else { @() };
            MTU=if ($validated) { $validated.MTU } else { $null }; CurrentBestRoutes=@($routes);
            ExpectedIPv4InterfaceMetric=0; MetricPolicyStore='ActiveStore'; RouteReady=$false }
        if ($Mode -eq 'Plan') { Write-UVPNStatus $recordPath $result; return [pscustomobject]$result }
        if ($Mode -eq 'Install' -and $existing) { throw 'The dedicated service already exists; it will not be replaced or stopped.' }
        if ($Mode -eq 'Install' -and $existingAdapter.Count) {
            throw 'An adapter with the dedicated name already exists; it is not owned by this installation.'
        }
        if ($Mode -eq 'Install' -and @($routes | Where-Object Status -ne 'Resolved').Count) {
            throw 'Cannot verify the current best route for every configured target.'
        }
        if (-not $admin) {
            if (-not $Elevate) { throw 'Administrator rights are required; use an elevated shell or explicitly add -Elevate for UAC.' }
            if ($null -ne $stream) { $stream.Dispose(); $stream = $null }
            Invoke-UVPNElevation @{ Mode=$Mode; TunnelConfig=$tunnelPath; GatewayConfig=$gatewayPath;
                Endpoint=$Endpoint; WireGuardExe=$exe; ResultPath=$recordPath }
            $result.Status = 'ElevatedProcessCompleted'
            return [pscustomobject]$result
        }
        if ($Mode -eq 'Uninstall' -and -not $existing) {
            $result.Status = 'NotInstalled'
        } else {
            $arguments = if ($Mode -eq 'Install') { @('/installtunnelservice', ('"' + $tunnelPath + '"')) }
                         else { @('/uninstalltunnelservice', $name) }
            $installationMayExist = $Mode -eq 'Install'
            $process = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
            if ($process.ExitCode -ne 0) { throw 'WireGuard reported failure; no existing tunnels were automatically changed.' }
            if ($Mode -eq 'Install') {
                $adapter = Wait-UVPNNewAdapter $name $serviceName $previousIdentities
                Set-UVPNNewAdapterPriority $adapter $name
                $selected = @(Get-UVPNTargetRoutes $validated.Destinations)
                if ($selected.Count -ne $validated.Destinations.Count -or
                    @($selected | Where-Object { $_.Status -ne 'Resolved' -or
                        [int]$_.InterfaceIndex -ne [int]$adapter.ifIndex -or $_.InterfaceAlias -cne $name }).Count) {
                    throw 'At least one configured target still selects a different interface.'
                }
                $result.CurrentBestRoutes = $selected
                $result.RouteReady = $true
                $result.Status = 'InstalledAndRoutesVerified'
            } else { $result.Status = 'UninstallCommandSucceeded' }
        }
        Write-UVPNStatus $recordPath $result
        return [pscustomobject]$result
    } catch {
        if ($installationMayExist) {
            Write-UVPNStatus $recordPath @{Mode=$Mode; Status='InstallationMayExistButRoutesNotReady'}
            throw ('Installation may have completed, but routing is not ready. Only this new tunnel may have changed; ' +
                'no other adapter or route was rolled back. Inspect the dedicated service before retrying. ' + $_.Exception.Message)
        }
        Write-UVPNStatus $recordPath @{Mode=$Mode; Status='Failed'}
        throw
    } finally { if ($null -ne $stream) { $stream.Dispose() } }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-UVPNGateway -Mode $Mode -TunnelConfig $TunnelConfig -GatewayConfig $GatewayConfig -Endpoint $Endpoint `
            -WireGuardExe $WireGuardExe -Elevate:$Elevate -ResultPath $ResultPath | ConvertTo-Json -Depth 5
    } catch {
        Write-Error ('windows-gateway: ' + $_.Exception.Message)
        exit 1
    }
}
