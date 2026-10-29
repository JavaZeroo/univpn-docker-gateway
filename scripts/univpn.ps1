<#
.SYNOPSIS
One local profile: configure, connect, inspect, stop or remove its Windows entry.
.NOTES
Passwords and SMS go only to vpnctl's interactive terminal. No login retry.
Stop keeps the dedicated Windows tunnel, keys and verified download cache.
Remove uninstalls only this named tunnel; it does not delete keys or volumes.
#>
[CmdletBinding()]
param(
    [ValidateSet('Connect','Init','Status','Stop','Remove')][string]$Mode='Connect',
    [ValidatePattern('^[a-z][a-z0-9-]{0,23}$')][string]$Name='lab',
    [string]$RootDirectory,
    [string]$GatewayConfig,
    [string]$Image='javazero/univpn-docker-gateway:latest',
    [string]$AccessSubnet='10.203.0.0/30',
    [ValidateRange(1024,65535)][int]$UdpPort=51829
)

$ErrorActionPreference='Stop'
# Windows PowerShell 5.1 can evaluate this automatic variable before binding
# script defaults; resolve the default once execution has entered the file.
if ([string]::IsNullOrWhiteSpace($RootDirectory)) { $RootDirectory=$PSScriptRoot }
$script:UVPNClientDotSourced=$MyInvocation.InvocationName -eq '.'
$script:UVPNClientOptions=@{Action=$Mode;ProfileName=$Name;Root=$RootDirectory;Config=$GatewayConfig;ProfileImage=$Image;Subnet=$AccessSubnet;Port=$UdpPort}
. (Join-Path $PSScriptRoot 'windows-profile.ps1')
. (Join-Path $PSScriptRoot 'windows-prerequisites.ps1')

function Invoke-UVPNDocker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed. Check Docker Desktop and this profile.' }
}

function Get-UVPNContainerState([string[]]$Compose) {
    $text = & docker @Compose exec -T vpn vpnctl status 2>$null
    if (-not $text) { throw 'VPN control is unavailable; check this profile container.' }
    try { return ($text -join "`n" | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'VPN control returned an invalid status.' }
}

function Write-UVPNCompose([string]$Directory,[string]$ProfileImage) {
    $file=Join-Path $Directory 'compose.yml'
    if (Test-Path -LiteralPath $file) { return $file }
    $imageJson=ConvertTo-Json -InputObject $ProfileImage -Compress
    $metadata=Get-Content -LiteralPath (Join-Path $Directory 'profile.json') -Raw | ConvertFrom-Json
    $port=[int]$metadata.udp_port
    if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid profile UDP port.' }
    $yaml=@"
services:
  vpn:
    image: $imageJson
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun:/dev/net/tun]
    sysctls:
      net.ipv4.ip_forward: "1"
    ports: ["127.0.0.1:${port}:51820/udp"]
    volumes:
      - ./vpn.json:/config/vpn.json:ro
      - ./wg0.conf:/config/wg0.conf:ro
      - vendor-cache:/var/cache/univpn
    tmpfs:
      - /run
      - /tmp
      - /root/UniVPN:mode=0700
      - /usr/local/UniVPN/log:mode=0700
      - /usr/local/UniVPN/serviceclient/log:mode=0700
    mem_limit: 384m
    cpus: 0.5
    pids_limit: 128
    restart: "no"
    logging:
      driver: local
      options:
        max-size: 1m
        max-file: "1"
volumes:
  vendor-cache:
"@
    [IO.File]::WriteAllText($file,$yaml.Replace("`r`n","`n")+"`n",[Text.UTF8Encoding]::new($false))
    return $file
}

function Wait-UVPNRuntime([string[]]$Compose) {
    Write-Host 'Preparing the VPN runtime. First use downloads about 30 MB from the vendor; later starts use the verified cache.'
    $deadline=[DateTime]::UtcNow.AddMinutes(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        & docker @Compose exec -T vpn test -S /run/univpn/control.sock 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        $container=& docker @Compose ps --all --format json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect this profile container.' }
        if ($container -and ($container -join "`n") -match '"State"\s*:\s*"exited"') {
            & docker @Compose logs --tail 10 vpn
            throw 'Runtime preparation failed. No login was submitted.'
        }
        Start-Sleep -Seconds 2
    }
    throw 'Runtime preparation timed out. Inspect the profile logs; no login was submitted.'
}

function Test-UVPNApplicationProbe($Probe) {
    $client=[Net.Sockets.TcpClient]::new()
    try {
        $pending=$client.BeginConnect([string]$Probe.host,[int]$Probe.port,$null,$null)
        if (-not $pending.AsyncWaitHandle.WaitOne(2000)) { return $false }
        $client.EndConnect($pending)
        $stream=$client.GetStream()
        $stream.ReadTimeout=2000
        $expected=[Text.Encoding]::UTF8.GetBytes([string]$Probe.banner_prefix)
        if ($expected.Length -eq 0 -or $expected.Length -gt 256) { return $false }
        $bytes=New-Object byte[] $expected.Length
        $read=0
        while ($read -lt $expected.Length) {
            $count=$stream.Read($bytes,$read,$expected.Length-$read)
            if ($count -eq 0) { return $false }
            $read+=$count
        }
        for ($index=0;$index -lt $expected.Length;$index++) {
            if ($bytes[$index] -ne $expected[$index]) { return $false }
        }
        return $true
    } catch { return $false }
    finally { $client.Dispose() }
}

function Invoke-UVPNClient {
    param([string]$Action,[string]$ProfileName,[string]$Root,[string]$Config,[string]$ProfileImage,[string]$Subnet,[int]$Port)
    if ($ProfileName -cnotmatch '^[a-z][a-z0-9-]{0,23}$') { throw 'Invalid profile name.' }
    if ($Action -in @('Connect','Init')) { $null=Ensure-UVPNWireGuard }
    $directory=Join-Path (Join-Path $Root 'private') $ProfileName
    $metadataPath=Join-Path $directory 'profile.json'
    if (-not (Test-Path -LiteralPath $metadataPath)) {
        if ($Action -notin @('Connect','Init')) { throw 'Profile not found. Run Connect first.' }
        $options=@{Mode='Init';Name=$ProfileName;RootDirectory=$Root;Image=$ProfileImage;AccessSubnet=$Subnet;UdpPort=$Port}
        if ($Config) { $options.GatewayConfig=$Config }
        $null=Invoke-UVPNProfile @options
    } elseif ($Action -eq 'Init') { throw 'Profile already exists; keys will not be overwritten.' }
    $metadata=Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    if ($metadata.schema_version -ne 1 -or $metadata.name -cne $ProfileName) { throw 'Profile metadata is invalid.' }
    $composeFile=Write-UVPNCompose $directory ([string]$metadata.image)
    $compose=@('compose','-f',$composeFile,'-p',('univpn-'+$ProfileName))
    $tunnel='univpn-'+$ProfileName
    $gatewayFile=Join-Path $directory 'vpn.json'
    $tunnelFile=Join-Path $directory ($tunnel+'.conf')
    $endpoint='127.0.0.1:'+([int]$metadata.udp_port)
    if ($Action -eq 'Init') { Write-Host ('Profile ready: '+$ProfileName); return }
    if ($Action -eq 'Stop') { Invoke-UVPNDocker ($compose+@('stop','vpn')); return }
    if ($Action -eq 'Remove') {
        $null=Invoke-UVPNGateway -Mode Uninstall -TunnelConfig $tunnelFile -Elevate
        Invoke-UVPNDocker ($compose+@('down'))
        return
    }
    if ($Action -eq 'Status') {
        $status=Get-UVPNContainerState $compose
        $status | Select-Object state,engine_alive,authenticated,tunnel_started,data_plane_ready | Format-List
        $plan=Invoke-UVPNGateway -Mode Plan -TunnelConfig $tunnelFile -GatewayConfig $gatewayFile -Endpoint $endpoint
        $plan.CurrentBestRoutes | Format-Table -AutoSize
        return
    }
    $null=Invoke-UVPNGateway -Mode Plan -TunnelConfig $tunnelFile -GatewayConfig $gatewayFile -Endpoint $endpoint
    Invoke-UVPNDocker ($compose+@('up','-d','--pull','missing'))
    Wait-UVPNRuntime $compose
    $status=Get-UVPNContainerState $compose
    if (-not $status.authenticated) {
        $command=if ($status.state -eq 'awaiting_sms') { 'sms' } elseif ($status.state -eq 'idle') { 'login' } else {
            throw 'The previous authentication ended. Run Stop, then Connect for one new explicit login.'
        }
        Invoke-UVPNDocker ($compose+@('exec','vpn','vpnctl',$command))
    }
    $deadline=[DateTime]::UtcNow.AddSeconds(40)
    do {
        $status=Get-UVPNContainerState $compose
        if ($status.data_plane_ready) { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $status.data_plane_ready) { throw 'Authentication completed but the real target probe is not ready; Windows entry was not installed.' }
    $existing=Get-Service -Name ('WireGuardTunnel$'+$tunnel) -ErrorAction SilentlyContinue
    if ($existing) {
        $plan=Invoke-UVPNGateway -Mode Plan -TunnelConfig $tunnelFile -GatewayConfig $gatewayFile -Endpoint $endpoint
        if ($existing.Status -ne 'Running' -or -not $plan.CurrentBestRoutes -or
            @($plan.CurrentBestRoutes | Where-Object { $_.Status -ne 'Resolved' -or $_.InterfaceAlias -cne $tunnel }).Count) {
            throw 'Existing dedicated tunnel is not ready. Inspect it or explicitly Remove; no service was overwritten.'
        }
    } else {
        $result=Join-Path $directory 'windows-result.json'
        $null=Invoke-UVPNGateway -Mode Install -TunnelConfig $tunnelFile -GatewayConfig $gatewayFile -Endpoint $endpoint -Elevate -ResultPath $result
        $installed=Get-Content -LiteralPath $result -Raw | ConvertFrom-Json
        if ($installed.Status -ne 'InstalledAndRoutesVerified') { throw 'Windows tunnel installation did not verify the target routes.' }
        $plan=Invoke-UVPNGateway -Mode Plan -TunnelConfig $tunnelFile -GatewayConfig $gatewayFile -Endpoint $endpoint
        if (-not $plan.CurrentBestRoutes -or
            @($plan.CurrentBestRoutes | Where-Object { $_.Status -ne 'Resolved' -or $_.InterfaceAlias -cne $tunnel }).Count) {
            throw 'The new Windows tunnel is not the best route to every target.'
        }
    }
    $probe=(Get-Content -LiteralPath $gatewayFile -Raw | ConvertFrom-Json).probe
    $deadline=[DateTime]::UtcNow.AddSeconds(15)
    do {
        $reachable=Test-UVPNApplicationProbe $probe
        if ($reachable) { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $reachable) { throw 'Container VPN and Windows routes are ready, but the Windows application probe failed. Inspect the dedicated tunnel; no authentication was retried.' }
    Write-Host ('Connected; Windows target banner verified. Use original IPs in your apps. Profile: '+$ProfileName)
}

if (-not $script:UVPNClientDotSourced) {
    try { Invoke-UVPNClient @script:UVPNClientOptions }
    catch { [Console]::Error.WriteLine('univpn: '+$_.Exception.Message); exit 1 }
}
