# Self-contained tests; all system/service/process operations are mocked.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/windows-gateway.ps1')

$script:Passed = 0
$script:NativeCalls = @()
$script:Admin = $false
$script:ServiceExists = $false
$script:RouteAvailable = $true
function Test-UVPNAdministrator { return $script:Admin }
function Get-UVPNExistingService($Name) {
    if ($script:ServiceExists -or ($script:InstallLaunched -and $script:ServiceReady)) {
        return [pscustomobject]@{Name=$Name;Status='Running'}
    }
    return $null
}
function Get-UVPNAdapters {
    [pscustomobject]@{Name='huawei-new';InterfaceDescription='WireGuard Tunnel';ifIndex=17;
        InterfaceGuid='11111111-1111-1111-1111-111111111111';Status='Up'}
    if ($script:AdapterAlreadyExists) {
        [pscustomobject]@{Name='UNIVPN-test';InterfaceDescription='WireGuard Tunnel';ifIndex=99;
            InterfaceGuid='22222222-2222-2222-2222-222222222222';Status='Up'}
    } elseif ($script:InstallLaunched -and $script:AdapterReady) {
        [pscustomobject]@{Name='univpn-test';InterfaceDescription=$script:AdapterDescription;ifIndex=99;
            InterfaceGuid=if ($script:ReusedIdentity) {'11111111-1111-1111-1111-111111111111'}
                          else {'33333333-3333-3333-3333-333333333333'};Status='Up'}
    }
}
function Get-UVPNClockMilliseconds { return $script:ClockTick }
function Start-Sleep { param($Milliseconds) $script:ClockTick += $Milliseconds }
function Set-NetIPInterface {
    param($InterfaceIndex,$AddressFamily,$AutomaticMetric,$InterfaceMetric,$PolicyStore,$ErrorAction)
    $script:MetricCalls += [pscustomobject]@{InterfaceIndex=$InterfaceIndex;AddressFamily=$AddressFamily;
        AutomaticMetric=$AutomaticMetric;InterfaceMetric=$InterfaceMetric;PolicyStore=$PolicyStore}
    $script:MetricSet = $true
}
function Get-NetIPInterface {
    param($InterfaceIndex,$AddressFamily,$PolicyStore,$ErrorAction)
    [pscustomobject]@{InterfaceIndex=$InterfaceIndex;AutomaticMetric='Disabled';InterfaceMetric=0}
}
function Get-UVPNOfficialExecutable($Path) { return 'C:\Program Files\WireGuard\wireguard.exe' }
function Get-UVPNTargetRoutes($Destinations) {
    foreach ($target in $Destinations) {
        $selected = $script:MetricSet -and $script:NewRouteSelected
        [pscustomobject]@{Target=$target;Status=if ($script:RouteAvailable) {'Resolved'} else {'Unavailable'};
            InterfaceAlias=if ($selected) {'univpn-test'} else {'existing-vpn'};
            InterfaceIndex=if ($selected) {99} else {17};DestinationPrefix=$target;NextHop='0.0.0.0';RouteMetric=0}
    }
}
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$Wait,[switch]$PassThru,$Verb,$ErrorAction)
    $script:NativeCalls += [pscustomobject]@{FilePath=$FilePath;Arguments=@($ArgumentList);Verb=$Verb;WindowStyle=$WindowStyle}
    if ($ArgumentList[0] -eq '/installtunnelservice') { $script:InstallLaunched = $true }
    return [pscustomobject]@{ExitCode=0}
}
function Assert($Condition, $Message) { if (-not $Condition) { throw ('Assertion: ' + $Message) } }
function Check($Name, [scriptblock]$Body) {
    $script:NativeCalls = @(); $script:Admin = $false; $script:ServiceExists = $false; $script:RouteAvailable = $true
    $script:MetricCalls = @(); $script:MetricSet = $false; $script:InstallLaunched = $false
    $script:ServiceReady = $true; $script:AdapterReady = $true; $script:AdapterAlreadyExists = $false
    $script:AdapterDescription = 'WireGuard Tunnel'; $script:ReusedIdentity = $false
    $script:NewRouteSelected = $true; $script:ClockTick = 0
    & $Body
    $script:Passed += 1
    Write-Output ('PASS ' + $Name)
}
function Reject([scriptblock]$Body) {
    $caught = $false
    try { & $Body | Out-Null } catch { $caught = $true }
    Assert $caught 'invalid operation was accepted'
    Assert ($script:NativeCalls.Count -eq 0) 'a rejected operation invoked a process'
    Assert ($script:MetricCalls.Count -eq 0) 'a rejected operation changed an interface'
}
function Reject-Partial([scriptblock]$Body) {
    $message = $null
    try { & $Body | Out-Null } catch { $message = $_.Exception.Message }
    Assert ($message -and $message.Contains('Installation may have completed, but routing is not ready')) 'partial installation was not explained'
    Assert ($script:NativeCalls.Count -eq 1) 'partial failure invoked an extra process or rollback'
    Assert (@($script:MetricCalls | Where-Object InterfaceIndex -ne 99).Count -eq 0) 'another adapter was modified'
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ('uvpn-windows-test-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $temp
$script:ConfigPath = Join-Path $temp 'univpn-test.conf'
$script:GatewayPath = Join-Path $temp 'vpn.json'
$script:ResultFile = Join-Path $temp 'result.json'
$private = [Convert]::ToBase64String([byte[]](1..32))
$public = [Convert]::ToBase64String([byte[]](33..64))
$script:Fixture = @"
[Interface]
Address = 10.203.0.2/32
PrivateKey = $private
MTU = 1280
[Peer]
PublicKey = $public
Endpoint = 127.0.0.1:51829
AllowedIPs = 10.80.0.11/32, 10.80.0.12/32
PersistentKeepalive = 25
"@
$gatewayText = '{"wireguard_client":"10.203.0.2/32","allowed_cidrs":["10.80.0.12/32","10.80.0.11/32"]}'
[IO.File]::WriteAllText($script:GatewayPath, $gatewayText)
function Put-Config([string]$Content=$script:Fixture) { [IO.File]::WriteAllText($script:ConfigPath, $Content) }
function Invoke-Case([string]$Mode='Plan', [switch]$Elevate, [string]$ResultPath) {
    Invoke-UVPNGateway -Mode $Mode -TunnelConfig $script:ConfigPath -GatewayConfig $script:GatewayPath `
        -Endpoint 127.0.0.1:51829 -Elevate:$Elevate -ResultPath $ResultPath
}

try {
    Check 'default plan is read-only and sanitized' {
        Put-Config
        $before = [IO.File]::ReadAllText($script:ConfigPath)
        $plan = Invoke-Case
        Assert ($plan.Mode -eq 'Plan' -and -not $plan.Administrator) 'plan did not work without admin'
        Assert ($plan.CurrentBestRoutes.Count -eq 2) 'target routes missing'
        Assert ($plan.ExpectedIPv4InterfaceMetric -eq 0 -and $plan.MetricPolicyStore -eq 'ActiveStore') 'expected priority absent'
        Assert ($script:NativeCalls.Count -eq 0) 'plan invoked a process'
        Assert ($script:MetricCalls.Count -eq 0) 'plan changed an interface'
        $serialized = $plan | ConvertTo-Json -Depth 5
        Assert (-not $serialized.Contains($private) -and -not $serialized.Contains($public)) 'plan exposed keys'
        Assert ([IO.File]::ReadAllText($script:ConfigPath) -ceq $before) 'plan changed config'
    }
    Check 'CIDR set must match exactly' {
        Put-Config ($script:Fixture.Replace('10.80.0.11/32, 10.80.0.12/32', '10.80.0.11/32'))
        Reject { Invoke-Case }
    }
    Check 'client address must match' {
        Put-Config ($script:Fixture.Replace('Address = 10.203.0.2/32', 'Address = 10.203.0.3/32'))
        Reject { Invoke-Case }
    }
    Check 'endpoint must be exact loopback and explicit' {
        Put-Config ($script:Fixture.Replace('127.0.0.1:51829', 'localhost:51829'))
        Reject { Invoke-Case }
        Put-Config
        Reject { Invoke-UVPNGateway -TunnelConfig $script:ConfigPath -GatewayConfig $script:GatewayPath }
    }
    Check 'default IPv4 and IPv6 destinations are rejected' {
        foreach ($route in @('0.0.0.0/0','::/0')) {
            Put-Config ($script:Fixture.Replace('10.80.0.11/32, 10.80.0.12/32', $route))
            Reject { Invoke-Case }
        }
    }
    Check 'DNS hooks extra peers and duplicate keys are rejected' {
        foreach ($extra in @('DNS = 1.1.1.1','PostUp = do-not-run','PreDown = do-not-run',
                             'Table = auto','Address = 10.203.0.2/32','[Peer]')) {
            Put-Config ($script:Fixture.Replace('[Peer]', ($extra + "`n[Peer]")))
            Reject { Invoke-Case }
        }
    }
    Check 'malformed placeholder and zero keys are rejected' {
        foreach ($key in @('REPLACE_WITH_PRIVATE_KEY','bad',[Convert]::ToBase64String([byte[]]::new(32)))) {
            Put-Config ($script:Fixture.Replace($private,$key))
            Reject { Invoke-Case }
        }
    }
    Check 'unsafe MTU and missing MTU are rejected' {
        foreach ($mtu in @('576','9000','bad')) {
            Put-Config ($script:Fixture.Replace('MTU = 1280', ('MTU = ' + $mtu)))
            Reject { Invoke-Case }
        }
        Put-Config ($script:Fixture.Replace('MTU = 1280',''))
        Reject { Invoke-Case }
    }
    Check 'existing named service is inspected but never replaced' {
        Put-Config; $script:ServiceExists = $true; $script:Admin = $true
        Assert (Invoke-Case).ServiceExists 'existing service missing from plan'
        Reject { Invoke-Case Install }
    }
    Check 'orphan pre-existing adapter name is rejected even with different case' {
        Put-Config; $script:AdapterAlreadyExists = $true; $script:Admin = $true
        Assert (Invoke-Case).AdapterExists 'existing adapter missing from plan'
        Reject { Invoke-Case Install }
    }
    Check 'install and uninstall refuse non-admin without explicit elevation' {
        Put-Config
        Reject { Invoke-Case Install }
        Reject { Invoke-Case Uninstall }
    }
    Check 'install only invokes dedicated service with config path' {
        Put-Config; $script:Admin = $true
        $result = Invoke-Case Install
        Assert ($result.Status -eq 'InstalledAndRoutesVerified' -and $result.RouteReady) 'install and route verification failed'
        Assert ($script:NativeCalls.Count -eq 1) 'unexpected process count'
        Assert ($script:NativeCalls[0].Arguments[0] -eq '/installtunnelservice') 'wrong command'
        Assert ($script:NativeCalls[0].Arguments[1].Contains('univpn-test.conf')) 'wrong config path'
        Assert (-not ($script:NativeCalls | ConvertTo-Json -Depth 4).Contains($private)) 'key in process arguments'
        Assert ($script:MetricCalls.Count -eq 1) 'unexpected metric operation count'
        $metric = $script:MetricCalls[0]
        Assert ($metric.InterfaceIndex -eq 99 -and $metric.AddressFamily -eq 'IPv4') 'wrong interface selected'
        Assert ($metric.AutomaticMetric -eq 'Disabled' -and $metric.InterfaceMetric -eq 0 -and
            $metric.PolicyStore -eq 'ActiveStore') 'wrong priority settings'
        Assert (@($result.CurrentBestRoutes | Where-Object InterfaceIndex -ne 99).Count -eq 0) 'target did not select new interface'
    }
    Check 'readiness timeout never changes an old adapter' {
        Put-Config; $script:Admin = $true; $script:ServiceReady = $false
        Reject-Partial { Invoke-Case Install }
        Assert ($script:MetricCalls.Count -eq 0) 'timeout changed an interface'
        Assert ($script:ClockTick -le 10000) 'readiness exceeded its bounded wait'
    }
    Check 'a missing new adapter times out without metric changes' {
        Put-Config; $script:Admin = $true; $script:AdapterReady = $false
        Reject-Partial { Invoke-Case Install }
        Assert ($script:MetricCalls.Count -eq 0) 'missing adapter changed an interface'
    }
    Check 'wrong adapter type and reused old GUID are rejected before mutation' {
        Put-Config; $script:Admin = $true; $script:AdapterDescription = 'Existing VPN TAP'
        Reject-Partial { Invoke-Case Install }
        Assert ($script:MetricCalls.Count -eq 0) 'non-WireGuard interface changed'
        $script:NativeCalls = @(); $script:InstallLaunched = $false
        $script:AdapterDescription = 'WireGuard Tunnel'; $script:ReusedIdentity = $true
        Reject-Partial { Invoke-Case Install }
        Assert ($script:MetricCalls.Count -eq 0) 'pre-existing adapter identity changed'
    }
    Check 'successful install but old best route is a partial failure' {
        Put-Config; $script:Admin = $true; $script:NewRouteSelected = $false
        Reject-Partial { Invoke-Case Install -ResultPath $script:ResultFile }
        $record = [IO.File]::ReadAllText($script:ResultFile) | ConvertFrom-Json
        Assert ($record.Status -eq 'InstallationMayExistButRoutesNotReady') 'result falsely declared full success'
        Assert ($script:MetricCalls.Count -eq 1 -and $script:MetricCalls[0].InterfaceIndex -eq 99) 'wrong metric changes'
    }
    Check 'uninstall can only remove its own prefixed basename' {
        $script:Admin = $true; $script:ServiceExists = $true
        $result = Invoke-UVPNGateway -Mode Uninstall -TunnelConfig (Join-Path $temp 'univpn-test.conf')
        Assert ($script:NativeCalls[0].Arguments[0] -eq '/uninstalltunnelservice') 'wrong removal command'
        Assert ($script:NativeCalls[0].Arguments[1] -ceq 'univpn-test') 'wrong service removed'
        Assert ($script:MetricCalls.Count -eq 0) 'uninstall changed adapter metrics'
        $script:NativeCalls = @()
        foreach ($name in @('huawei-new.conf','mihomo.conf','UniVPN.conf','univpn-.conf','univpn-test.conf.dpapi')) {
            Reject { Invoke-UVPNGateway -Mode Uninstall -TunnelConfig (Join-Path $temp $name) }
        }
    }
    Check 'failed route lookup blocks install' {
        Put-Config; $script:Admin = $true; $script:RouteAvailable = $false
        Reject { Invoke-Case Install }
    }
    Check 'elevation is hidden explicit and contains no key content' {
        Put-Config
        $result = Invoke-Case Install -Elevate
        Assert ($script:NativeCalls.Count -eq 1) 'elevation did not invoke exactly once'
        $call = $script:NativeCalls[0]
        Assert ($call.Verb -eq 'RunAs' -and $call.WindowStyle -eq 'Hidden') 'wrong elevation policy'
        $policyIndex = [Array]::IndexOf($call.Arguments, '-ExecutionPolicy')
        Assert ($policyIndex -ge 0 -and $call.Arguments[$policyIndex + 1] -eq 'Bypass') 'child lacks process-only policy option'
        $encoded = $call.Arguments[-1]
        $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
        Assert ($command.Contains('univpn-test.conf') -and $command.Contains('vpn.json')) 'child paths missing'
        Assert (-not $command.Contains($private) -and -not $command.Contains($public)) 'key in child command'
        Assert (-not $command.Contains('-Elevate')) 'child could recursively elevate'
        Assert (-not $command.Contains('Set-ExecutionPolicy')) 'child modifies persistent execution policy'
        $parseTokens = $null; $parseErrors = $null
        $null = [Management.Automation.Language.Parser]::ParseInput($command, [ref]$parseTokens, [ref]$parseErrors)
        Assert ($parseErrors.Count -eq 0) 'encoded child command is not valid PowerShell'
    }
    Check 'non-admin uninstall can explicitly elevate without opening a config' {
        $missingPath = Join-Path $temp 'univpn-removed.conf'
        $result = Invoke-UVPNGateway -Mode Uninstall -TunnelConfig $missingPath -Elevate
        Assert ($result.Status -eq 'ElevatedProcessCompleted') 'uninstall elevation failed'
        Assert ($script:NativeCalls.Count -eq 1) 'unexpected elevation count'
        $call = $script:NativeCalls[0]
        Assert ($call.Verb -eq 'RunAs' -and $call.WindowStyle -eq 'Hidden') 'wrong elevation policy'
        $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($call.Arguments[-1]))
        Assert ($command.Contains("'Uninstall'") -and $command.Contains('univpn-removed.conf')) 'wrong child action'
    }
    Check 'optional result contains only status and cannot replace inputs' {
        Put-Config
        $null = Invoke-Case -ResultPath $script:ResultFile
        $record = [IO.File]::ReadAllText($script:ResultFile)
        Assert (-not $record.Contains('10.80.') -and -not $record.Contains($private)) 'result exposed config'
        Assert (@(($record | ConvertFrom-Json).PSObject.Properties.Name).Count -eq 3) 'result contains extra fields'
        Reject { Invoke-Case -ResultPath $script:GatewayPath }
        Reject { Invoke-Case -ResultPath $script:ConfigPath }
    }
    Write-Output ("All $script:Passed Windows gateway checks passed; no system changes or UAC calls.")
} finally {
    # Only remove files created by this test in its verified unique temp folder.
    foreach ($path in @($script:ConfigPath,$script:GatewayPath,$script:ResultFile)) {
        if ([IO.Path]::GetDirectoryName($path) -eq $temp -and [IO.File]::Exists($path)) { [IO.File]::Delete($path) }
    }
    [IO.Directory]::Delete($temp)
}
