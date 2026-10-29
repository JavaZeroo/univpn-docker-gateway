# Lifecycle checks: no real Docker, credentials, TCP connection, service or UAC.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../scripts/univpn.ps1') -Mode Status -Name demo
if ($script:UVPNClientOptions.Action -ne 'Status' -or $script:UVPNClientOptions.ProfileName -ne 'demo') {
    throw 'Dot-sourced dependency parameters overwrote the client invocation.'
}
if ([string]::IsNullOrWhiteSpace($script:UVPNClientOptions.Root) -or
    [IO.Path]::GetFullPath($script:UVPNClientOptions.Root) -ne [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../scripts'))) {
    throw 'Default client root must resolve to its own script directory on Windows PowerShell 5.1.'
}
$script:Count=1
$script:Fixture=Join-Path ([IO.Path]::GetTempPath()) ('univpn-client-test-'+[Guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $script:Fixture
function Assert-Client($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Ensure-UVPNWireGuard { return [pscustomobject]@{Reused=$true} }
function Invoke-UVPNDocker { param([string[]]$Arguments) $script:DockerCalls.Add(($Arguments -join '|')) }
function Wait-UVPNRuntime($Compose) { $script:Waited=$true }
function Get-UVPNContainerState($Compose) {
    $script:Reads++
    if ($script:Reads -eq 1 -and $script:InitialState -ne 'authenticated') {
        return [pscustomobject]@{state=$script:InitialState;authenticated=$false;data_plane_ready=$false}
    }
    return [pscustomobject]@{state='authenticated';authenticated=$true;data_plane_ready=$true}
}
function Test-UVPNApplicationProbe($Probe) { $script:ApplicationProbed=$true; return $true }
function Get-Service { param($Name,$ErrorAction) if ($script:Existing) { return [pscustomobject]@{Status='Running'} } }
function Invoke-UVPNGateway {
    param($Mode,$TunnelConfig,$GatewayConfig,$Endpoint,[switch]$Elevate,$ResultPath)
    $script:GatewayCalls.Add($Mode)
    if ($Mode -eq 'Install') { Write-UVPNStatus $ResultPath @{Mode='Install';Status='InstalledAndRoutesVerified';RouteReady=$true} }
    return [pscustomobject]@{CurrentBestRoutes=@([pscustomobject]@{Status='Resolved';InterfaceAlias='univpn-demo'})}
}
function Reset-Client([string]$State='idle',[bool]$Exists=$false) {
    $script:DockerCalls=[Collections.Generic.List[string]]::new()
    $script:GatewayCalls=[Collections.Generic.List[string]]::new()
    $script:Reads=0; $script:InitialState=$State; $script:Existing=$Exists
    $script:Waited=$false; $script:ApplicationProbed=$false
}
try {
    $directory=Join-Path $script:Fixture 'private/demo'
    $null=New-Item -ItemType Directory -Path $directory
    [IO.File]::WriteAllText((Join-Path $directory 'profile.json'),'{"schema_version":1,"name":"demo","image":"javazero/univpn-docker-gateway:latest","udp_port":51829,"access_subnet":"10.203.0.0/30"}')
    [IO.File]::WriteAllText((Join-Path $directory 'vpn.json'),'{"probe":{"host":"10.80.0.11","port":22,"banner_prefix":"SSH-2.0-"}}')
    $arguments=@{ProfileName='demo';Root=$script:Fixture;ProfileImage='ignored:current-profile-is-authoritative';Subnet='10.203.0.0/30';Port=51829}
    Reset-Client
    Invoke-UVPNClient -Action Connect @arguments
    Assert-Client ($script:DockerCalls.Count -eq 2 -and $script:DockerCalls[0].EndsWith('up|-d|--pull|missing') -and $script:DockerCalls[1].EndsWith('exec|vpn|vpnctl|login')) 'Connect must start and explicitly login once.'
    Assert-Client ($script:GatewayCalls -contains 'Install') 'Initial Connect must use the existing installer.'
    Assert-Client ($script:Waited -and $script:ApplicationProbed) 'Connect must await runtime and check Windows data path.'
    $yaml=Get-Content -LiteralPath (Join-Path $directory 'compose.yml') -Raw
    Assert-Client ($yaml.Contains('vendor-cache:/var/cache/univpn') -and $yaml.Contains('./wg0.conf:/config/wg0.conf:ro')) 'Compose must cache downloads and bind only server configuration.'
    Assert-Client (-not $yaml.Contains('univpn-demo.conf') -and -not $yaml.Contains('build:')) 'Windows private key and local build must not enter the container workflow.'
    $script:Count++

    Reset-Client awaiting_sms
    Invoke-UVPNClient -Action Connect @arguments
    Assert-Client ($script:DockerCalls[1].EndsWith('exec|vpn|vpnctl|sms') -and @($script:DockerCalls | Where-Object { $_.EndsWith('|login') }).Count -eq 0) 'Pending SMS must continue the current challenge without login replay.'
    $script:Count++

    Reset-Client authenticated $true
    Invoke-UVPNClient -Action Connect @arguments
    Assert-Client ($script:DockerCalls.Count -eq 1 -and -not ($script:GatewayCalls -contains 'Install')) 'Connected profile must not login or reinstall its interface.'
    $script:Count++

    Reset-Client failed
    $rejected=$false
    try { Invoke-UVPNClient -Action Connect @arguments } catch { $rejected=$true }
    Assert-Client ($rejected -and $script:DockerCalls.Count -eq 1 -and -not ($script:GatewayCalls -contains 'Install')) 'Ended authentication must fail without automatic password/SMS retry.'
    $script:Count++

    Reset-Client
    Invoke-UVPNClient -Action Stop @arguments
    Assert-Client ($script:DockerCalls.Count -eq 1 -and $script:DockerCalls[0].EndsWith('stop|vpn') -and $script:GatewayCalls.Count -eq 0) 'Stop must keep the interface, keys and cache.'
    $script:Count++

    Reset-Client
    Invoke-UVPNClient -Action Remove @arguments
    Assert-Client ($script:GatewayCalls[0] -eq 'Uninstall' -and $script:DockerCalls[0].EndsWith('|down') -and -not $script:DockerCalls[0].EndsWith('|-v')) 'Remove must target only this tunnel and preserve volumes.'
    $script:Count++

    Reset-Client
    $rejected=$false
    try { Invoke-UVPNClient -Action Connect -ProfileName '../other' -Root $script:Fixture } catch { $rejected=$true }
    Assert-Client ($rejected -and $script:DockerCalls.Count -eq 0) 'Invalid profile path must be rejected before Docker.'
    $script:Count++
    Write-Host ("PASS: {0} client lifecycle groups; no network or administrator changes." -f $script:Count)
} finally {
    $resolved=(Resolve-Path -LiteralPath $script:Fixture).Path
    $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if (-not $resolved.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolved -Leaf) -notlike 'univpn-client-test-*') { throw 'Unsafe test cleanup target.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
