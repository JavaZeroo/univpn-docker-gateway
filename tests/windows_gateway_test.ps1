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
    if ($script:ServiceExists) { return [pscustomobject]@{Name=$Name;Status='Running'} }
    return $null
}
function Get-UVPNOfficialExecutable($Path) { return 'C:\Program Files\WireGuard\wireguard.exe' }
function Get-UVPNTargetRoutes($Destinations) {
    foreach ($target in $Destinations) {
        [pscustomobject]@{Target=$target;Status=if ($script:RouteAvailable) {'Resolved'} else {'Unavailable'};
            InterfaceAlias='existing-vpn';InterfaceIndex=17;DestinationPrefix='10.80.0.0/24';NextHop='0.0.0.0';RouteMetric=10}
    }
}
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$Wait,[switch]$PassThru,$Verb,$ErrorAction)
    $script:NativeCalls += [pscustomobject]@{FilePath=$FilePath;Arguments=@($ArgumentList);Verb=$Verb;WindowStyle=$WindowStyle}
    return [pscustomobject]@{ExitCode=0}
}
function Assert($Condition, $Message) { if (-not $Condition) { throw ('Assertion: ' + $Message) } }
function Check($Name, [scriptblock]$Body) {
    $script:NativeCalls = @(); $script:Admin = $false; $script:ServiceExists = $false; $script:RouteAvailable = $true
    & $Body
    $script:Passed += 1
    Write-Output ('PASS ' + $Name)
}
function Reject([scriptblock]$Body) {
    $caught = $false
    try { & $Body | Out-Null } catch { $caught = $true }
    Assert $caught 'invalid operation was accepted'
    Assert ($script:NativeCalls.Count -eq 0) 'a rejected operation invoked a process'
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
        Assert ($script:NativeCalls.Count -eq 0) 'plan invoked a process'
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
    Check 'install and uninstall refuse non-admin without explicit elevation' {
        Put-Config
        Reject { Invoke-Case Install }
        Reject { Invoke-Case Uninstall }
    }
    Check 'install only invokes dedicated service with config path' {
        Put-Config; $script:Admin = $true
        $result = Invoke-Case Install
        Assert ($result.Status -eq 'InstallCommandSucceeded') 'install command failed'
        Assert ($script:NativeCalls.Count -eq 1) 'unexpected process count'
        Assert ($script:NativeCalls[0].Arguments[0] -eq '/installtunnelservice') 'wrong command'
        Assert ($script:NativeCalls[0].Arguments[1].Contains('univpn-test.conf')) 'wrong config path'
        Assert (-not ($script:NativeCalls | ConvertTo-Json -Depth 4).Contains($private)) 'key in process arguments'
    }
    Check 'uninstall can only remove its own prefixed basename' {
        $script:Admin = $true; $script:ServiceExists = $true
        $result = Invoke-UVPNGateway -Mode Uninstall -TunnelConfig (Join-Path $temp 'univpn-test.conf')
        Assert ($script:NativeCalls[0].Arguments[0] -eq '/uninstalltunnelservice') 'wrong removal command'
        Assert ($script:NativeCalls[0].Arguments[1] -ceq 'univpn-test') 'wrong service removed'
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
