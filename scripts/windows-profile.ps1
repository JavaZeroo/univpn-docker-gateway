<#
.SYNOPSIS
Create one local VPN profile and paired WireGuard configs, then run a read-only plan.
.EXAMPLE
./windows-profile.ps1 -Mode Init -Name lab -GatewayConfig ./vpn.json
.EXAMPLE
./windows-profile.ps1 -Mode Init -Name lab -AccessSubnet 10.203.1.0/30 -UdpPort 51830
.EXAMPLE
./windows-profile.ps1 -Mode Plan -Name lab
.NOTES
No VPN passwords are requested or saved. Existing profile directories are never
overwritten. Keys come from the signed local WireGuard wg.exe, remain in restricted
files, and are never printed. This script does not start Docker or install tunnels.
Compose generation, image pulling and lifecycle belong to the outer client entry.
#>
[CmdletBinding()]
param(
    [ValidateSet('Init','Plan')][string]$Mode='Init',
    [string]$Name,
    [string]$RootDirectory=$PSScriptRoot,
    [string]$ProfileDirectory,
    [string]$GatewayConfig,
    [string]$AccessSubnet='10.203.0.0/30',
    [int]$UdpPort=51829,
    [string]$Image='javazero/univpn-docker-gateway:latest',
    [string]$WgExe,
    [string]$Gateway,
    [int]$GatewayPort=443,
    [string[]]$AllowedCidrs,
    [string]$ProbeHost,
    [int]$ProbePort=22
)

# Dot-sourcing the existing script declares its own parameters in this scope.
# Preserve our invocation first; shared validation/Plan functions remain reused.
$script:UVPNProfileEntryOptions = @{}
foreach ($key in $PSBoundParameters.Keys) { $script:UVPNProfileEntryOptions[$key] = $PSBoundParameters[$key] }
$script:UVPNProfileDefaultRoot = $PSScriptRoot
$script:UVPNProfileDotSourced = $MyInvocation.InvocationName -eq '.'
. (Join-Path $PSScriptRoot 'windows-gateway.ps1')

function Assert-UVPNProfileName([string]$Value) {
    if ($Value -cnotmatch '^[a-z0-9][a-z0-9_-]{0,24}$') {
        throw 'Use a profile name of 1..25 lowercase letters, digits, hyphens or underscores.'
    }
}

function Assert-UVPNProfileImage([string]$Value) {
    if ($Value -cnotmatch '^[a-z0-9][a-z0-9._/:-]*(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})?$' -or $Value.Contains('..')) {
        throw 'Use a Docker image reference without whitespace or shell characters.'
    }
}

function ConvertFrom-UVPNIPv4Number([uint64]$Value) {
    return (@(24,16,8,0 | ForEach-Object { [string](($Value -shr $_) -band 255) }) -join '.')
}

function Get-UVPNProfileLink([string]$Subnet) {
    $range = ConvertTo-UVPNCidr $Subnet -Network
    $first = $range.Start -shr 24
    $second = ($range.Start -shr 16) -band 255
    $private = $first -eq 10 -or ($first -eq 172 -and $second -ge 16 -and $second -le 31) -or
        ($first -eq 192 -and $second -eq 168)
    if ($range.Prefix -ne 30 -or -not $private) { throw 'The access subnet must be a private canonical IPv4 /30.' }
    return [pscustomobject]@{ Range=$range; Subnet=$Subnet;
        Server=((ConvertFrom-UVPNIPv4Number ($range.Start + 1)) + '/30');
        Client=((ConvertFrom-UVPNIPv4Number ($range.Start + 2)) + '/32') }
}

function Read-UVPNProfileGateway([string]$Path, $Link, [string]$Gateway, [int]$GatewayPort,
                               [string[]]$AllowedCidrs, [string]$ProbeHost, [int]$ProbePort,
                               [bool]$GatewayPortExplicit=$false, [bool]$ProbePortExplicit=$false) {
    if ($Path) {
        try { $source = [IO.File]::ReadAllText((Resolve-UVPNFilePath $Path -MustExist)) | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'GatewayConfig could not be read as JSON.' }
        if (-not $source -or $source -is [array]) { throw 'GatewayConfig must be a JSON object.' }
        foreach ($property in $source.PSObject.Properties.Name) {
            if ($property -match '^(username|user|password|vpn_password|otp|token|secret|private_?key)$') {
                throw 'GatewayConfig must not contain accounts, passwords, tokens or private keys.'
            }
        }
        $Gateway = [string]$source.gateway
        $port = if ($source.PSObject.Properties['port']) { $source.port } else { 443 }
        if ([string]$port -notmatch '^[0-9]{1,5}$') { throw 'Invalid VPN gateway port.' }
        $GatewayPort = [int]$port
        if (-not $source.PSObject.Properties['allowed_cidrs'] -or $source.allowed_cidrs -is [string]) {
            throw 'GatewayConfig needs an array of allowed_cidrs.'
        }
        $AllowedCidrs = @($source.allowed_cidrs | ForEach-Object { [string]$_ })
        if (-not $source.PSObject.Properties['probe'] -or -not $source.probe) { throw 'GatewayConfig needs a real banner probe.' }
        $ProbeHost = [string]$source.probe.host
        $port = $source.probe.port
        if ([string]$port -notmatch '^[0-9]{1,5}$') { throw 'Invalid probe port.' }
        $ProbePort = [int]$port
        $banner = [string]$source.probe.banner_prefix
    } else {
        if (-not $Gateway) {
            $answer = Read-Host 'VPN gateway hostname or IPv4[:port] (no account/password)'
            if ($answer -notmatch '^([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?$') {
                throw 'Use a gateway hostname or IPv4, optionally followed by a valid port; IPv6 is not supported.'
            }
            $Gateway = $Matches[1]
            $inlinePort = $Matches[2]
            if ($inlinePort) {
                if ([int]$inlinePort -lt 1 -or [int]$inlinePort -gt 65535) { throw 'Invalid VPN gateway port.' }
                if ($GatewayPortExplicit -and [int]$inlinePort -ne $GatewayPort) {
                    throw 'Inline gateway port conflicts with the explicit GatewayPort parameter.'
                }
                $GatewayPort = [int]$inlinePort
            } elseif (-not $GatewayPortExplicit) {
                $portAnswer = Read-Host ('VPN gateway port [' + $GatewayPort + ']')
                if ($portAnswer) {
                    if ($portAnswer -notmatch '^[0-9]{1,5}$') { throw 'Invalid VPN gateway port.' }
                    $GatewayPort = [int]$portAnswer
                }
            }
        }
        if (-not $AllowedCidrs -or -not $AllowedCidrs.Count) {
            $AllowedCidrs = @((Read-Host 'Allowed target IPv4 CIDRs, separated by commas') -split ',' | ForEach-Object { $_.Trim() })
        }
        if (-not $ProbeHost) {
            $defaultProbe = if ($AllowedCidrs.Count -eq 1 -and $AllowedCidrs[0] -match '/32$') {
                $AllowedCidrs[0].Split('/')[0]
            } else { '' }
            $answer = Read-Host ('SSH banner probe IPv4[:port]' + $(if ($defaultProbe) { ' [' + $defaultProbe + ']' }))
            if ($answer) {
                if ($answer -notmatch '^([0-9.]+)(?::([0-9]{1,5}))?$') { throw 'Use a probe IPv4, optionally followed by a valid port.' }
                $ProbeHost = $Matches[1]
                $inlinePort = $Matches[2]
                if ($inlinePort) {
                    if ([int]$inlinePort -lt 1 -or [int]$inlinePort -gt 65535) { throw 'Invalid probe port.' }
                    if ($ProbePortExplicit -and [int]$inlinePort -ne $ProbePort) { throw 'Inline probe port conflicts with the explicit ProbePort parameter.' }
                    $ProbePort = [int]$inlinePort
                }
            } else { $ProbeHost = $defaultProbe }
        }
        $banner = 'SSH-2.0-'
    }
    if ($Gateway -notmatch '^[A-Za-z0-9.-]+$' -or $GatewayPort -lt 1 -or $GatewayPort -gt 65535 -or
        $ProbePort -lt 1 -or $ProbePort -gt 65535) { throw 'Invalid VPN gateway or probe endpoint.' }
    if ($Gateway -match '^[0-9.]+$') { $null = ConvertTo-UVPNCidr ($Gateway + '/32') }
    $ranges = @($AllowedCidrs | ForEach-Object { ConvertTo-UVPNCidr $_ -Network })
    if (-not $ranges.Count -or @($ranges.Text | Sort-Object -Unique).Count -ne $ranges.Count) {
        throw 'Provide a nonempty, unique list of target CIDRs.'
    }
    foreach ($range in $ranges) {
        if ($range.Start -le $Link.Range.End -and $Link.Range.Start -le $range.End) {
            throw 'A target CIDR overlaps the WireGuard access subnet.'
        }
    }
    $probe = ConvertTo-UVPNCidr ($ProbeHost + '/32')
    if (-not @($ranges | Where-Object { $probe.Start -ge $_.Start -and $probe.Start -le $_.End }).Count -or
        -not $banner -or $banner.Length -gt 128 -or $banner -match '[\x00-\x1f]') {
        throw 'The banner probe must be inside an allowed CIDR and use a nonempty safe prefix.'
    }
    $config = [ordered]@{ gateway=$Gateway; port=$GatewayPort; vpn_interface='cnem_vnic';
        allowed_cidrs=@($ranges.Text); wireguard_client=$Link.Client;
        probe=[ordered]@{host=$ProbeHost;port=$ProbePort;banner_prefix=$banner} }
    if ($Path) {
        if ($source.PSObject.Properties['vpn_interface'] -and $source.vpn_interface -ne 'cnem_vnic') {
            throw 'This profile entry supports the native cnem_vnic interface only.'
        }
        if ($source.PSObject.Properties['approved_certificate_sha256']) {
            $pin = $source.approved_certificate_sha256
            if ($null -ne $pin -and ([string]$pin -cnotmatch '^[0-9a-fA-F]{64}$')) { throw 'Invalid approved certificate SHA256.' }
            if ($null -ne $pin) { $config['approved_certificate_sha256'] = [string]$pin }
        }
        foreach ($key in @('tunnel_mode','gm_algorithm','route_cover')) {
            if ($source.PSObject.Properties[$key]) {
                $value = $source.$key
                $choices = if ($key -eq 'tunnel_mode') { @('1','2','3') } else { @('0','1') }
                if ([string]$value -notin $choices) { throw 'Invalid native tunnel option.' }
                $config[$key] = [int]$value
            }
        }
    }
    return $config
}

function Protect-UVPNProfileDirectory([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
                            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Get-UVPNProfileWgExecutable([string]$Path) {
    if (-not $Path) { $Path = Join-Path $env:ProgramFiles 'WireGuard/wg.exe' }
    $resolved = Resolve-UVPNFilePath $Path -MustExist
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved -ErrorAction Stop
    if ([IO.Path]::GetFileName($resolved) -ine 'wg.exe' -or $signature.Status -ne 'Valid' -or
        -not $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne 'WireGuard LLC') {
        throw 'Use a local wg.exe with a valid WireGuard LLC signature.'
    }
    return $resolved
}

function Invoke-UVPNProfileKeyCommand([string]$Executable, [ValidateSet('genkey','pubkey')][string]$Command, [string]$InputKey) {
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = $Command
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardInput = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        if (-not $process.Start()) { throw 'Key process failed.' }
        if ($Command -eq 'pubkey') { $process.StandardInput.WriteLine($InputKey) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(5000)) { $process.Kill(); throw 'Key process timed out.' }
        $result = $process.StandardOutput.ReadToEnd().Trim()
        $null = $process.StandardError.ReadToEnd()
        if ($process.ExitCode -ne 0) { throw 'Key process failed.' }
        Assert-UVPNKey $result
        return $result
    } catch { throw 'WireGuard key generation failed; no key material is printed.' }
    finally { $process.Dispose() }
}

function New-UVPNProfileKeys([string]$Executable) {
    $server = Invoke-UVPNProfileKeyCommand $Executable genkey
    $client = Invoke-UVPNProfileKeyCommand $Executable genkey
    $serverPublic = Invoke-UVPNProfileKeyCommand $Executable pubkey $server
    $clientPublic = Invoke-UVPNProfileKeyCommand $Executable pubkey $client
    return [pscustomobject]@{ServerPrivate=$server;ServerPublic=$serverPublic;ClientPrivate=$client;ClientPublic=$clientPublic}
}

function Write-UVPNProfileFile([string]$Path, [string]$Content) {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Content.Replace("`r`n", "`n"))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes,0,$bytes.Length) }
    finally { $stream.Dispose(); [Array]::Clear($bytes,0,$bytes.Length) }
}

function Invoke-UVPNProfile {
    [CmdletBinding()]
    param([ValidateSet('Init','Plan')][string]$Mode='Init', [string]$Name,
          [string]$RootDirectory=$script:UVPNProfileDefaultRoot, [string]$ProfileDirectory, [string]$GatewayConfig,
          [string]$AccessSubnet='10.203.0.0/30', [int]$UdpPort=51829,
          [string]$Image='javazero/univpn-docker-gateway:latest', [string]$WgExe,
          [string]$Gateway, [int]$GatewayPort=443, [string[]]$AllowedCidrs, [string]$ProbeHost, [int]$ProbePort=22)
    $ErrorActionPreference = 'Stop'
    if (-not $Name -and -not $ProfileDirectory) { $Name = Read-Host 'Local profile name (for example lab)' }
    if ($Name) { Assert-UVPNProfileName $Name }
    $root = Resolve-UVPNFilePath $RootDirectory
    if (-not [IO.Directory]::Exists($root)) { throw 'RootDirectory must be an existing local client directory.' }
    $directory = if ($ProfileDirectory) { Resolve-UVPNFilePath $ProfileDirectory }
                 elseif ($Name) { Join-Path (Join-Path $root 'private') $Name }
                 else { throw 'Provide a name or explicit profile directory.' }
    $manifestPath = Join-Path $directory 'profile.json'
    if ($Mode -eq 'Plan') {
        try { $manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'The local profile manifest could not be read.' }
        Assert-UVPNProfileName ([string]$manifest.name)
        if ($manifest.schema_version -ne 1 -or ($Name -and $Name -cne $manifest.name)) { throw 'Profile identity mismatch.' }
        $Name = [string]$manifest.name; $Image = [string]$manifest.image
        $UdpPort = [int]$manifest.udp_port; $AccessSubnet = [string]$manifest.access_subnet
    } elseif ([IO.Directory]::Exists($directory) -or [IO.File]::Exists($directory)) {
        throw 'The profile path already exists; existing config and keys will not be overwritten.'
    }
    Assert-UVPNProfileName $Name
    Assert-UVPNProfileImage $Image
    if ($UdpPort -lt 1 -or $UdpPort -gt 65535) { throw 'UDP port must be between 1 and 65535.' }
    $link = Get-UVPNProfileLink $AccessSubnet
    $endpoint = '127.0.0.1:' + $UdpPort
    $vpnPath = Join-Path $directory 'vpn.json'
    $serverPath = Join-Path $directory 'wg0.conf'
    $tunnelPath = Join-Path $directory ('univpn-' + $Name + '.conf')
    if ($Mode -eq 'Init') {
        $config = Read-UVPNProfileGateway $GatewayConfig $link $Gateway $GatewayPort $AllowedCidrs $ProbeHost $ProbePort `
            ($PSBoundParameters.ContainsKey('GatewayPort')) ($PSBoundParameters.ContainsKey('ProbePort'))
        $wg = Get-UVPNProfileWgExecutable $WgExe
        $null = New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($directory)) -Force
        $null = New-Item -ItemType Directory -Path $directory -ErrorAction Stop
        Protect-UVPNProfileDirectory $directory
        $keys = $null
        try {
            $keys = New-UVPNProfileKeys $wg
            foreach ($key in @($keys.ServerPrivate,$keys.ServerPublic,$keys.ClientPrivate,$keys.ClientPublic)) { Assert-UVPNKey $key }
            if (@($keys.ServerPrivate,$keys.ServerPublic,$keys.ClientPrivate,$keys.ClientPublic | Sort-Object -Unique).Count -ne 4) {
                throw 'WireGuard did not produce distinct paired keys.'
            }
            $serverText = "[Interface]`nAddress = $($link.Server)`nPrivateKey = $($keys.ServerPrivate)`nListenPort = 51820`nMTU = 1280`nTable = off`n`n[Peer]`nPublicKey = $($keys.ClientPublic)`nAllowedIPs = $($link.Client)`n"
            $clientText = "[Interface]`nAddress = $($link.Client)`nPrivateKey = $($keys.ClientPrivate)`nMTU = 1280`n`n[Peer]`nPublicKey = $($keys.ServerPublic)`nEndpoint = $endpoint`nAllowedIPs = $($config.allowed_cidrs -join ', ')`nPersistentKeepalive = 25`n"
            Write-UVPNProfileFile $vpnPath ($config | ConvertTo-Json -Depth 5)
            Write-UVPNProfileFile $serverPath $serverText
            Write-UVPNProfileFile $tunnelPath $clientText
            $manifest = [ordered]@{schema_version=1;name=$Name;image=$Image;udp_port=$UdpPort;access_subnet=$AccessSubnet}
            Write-UVPNProfileFile $manifestPath ($manifest | ConvertTo-Json)
        } finally { $keys=$null; $serverText=$null; $clientText=$null; $key=$null }
    }
    # The same read-only validator/installer's Plan is the single authority.
    $plan = Invoke-UVPNGateway -Mode Plan -TunnelConfig $tunnelPath -GatewayConfig $vpnPath -Endpoint $endpoint
    return [pscustomobject]@{Name=$Name;ProjectName=('univpn-' + $Name);ProfileDirectory=$directory;
        GatewayConfig=$vpnPath;ServerConfig=$serverPath;TunnelConfig=$tunnelPath;
        Endpoint=$endpoint;UdpPort=$UdpPort;AccessSubnet=$AccessSubnet;Image=$Image;Plan=$plan}
}

if (-not $script:UVPNProfileDotSourced) {
    try { Invoke-UVPNProfile @script:UVPNProfileEntryOptions | ConvertTo-Json -Depth 7 }
    catch { Write-Error ('windows-profile: ' + $_.Exception.Message); exit 1 }
}
