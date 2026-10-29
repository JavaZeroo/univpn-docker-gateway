# Pure local fixtures. No WireGuard process, network, service, UAC or real ACL changes.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/windows-profile.ps1')

$script:Passed=0; $script:Events=@(); $script:KeyCalls=0; $script:PlanCalls=0
$script:ProtectedPath=$null
$script:FailAcl=$false; $script:FailKeygen=$false
$script:Answers=@()
$script:Keys=[pscustomobject]@{
    ServerPrivate=[Convert]::ToBase64String([byte[]](1..32));
    ServerPublic=[Convert]::ToBase64String([byte[]](33..64));
    ClientPrivate=[Convert]::ToBase64String([byte[]](65..96));
    ClientPublic=[Convert]::ToBase64String([byte[]](97..128)) }
function Protect-UVPNProfileDirectory($Path) {
    if (-not [IO.Directory]::Exists($Path) -or @(Get-ChildItem -LiteralPath $Path).Count) {
        throw 'ACL must precede every generated file.'
    }
    if ($script:FailAcl) { throw 'Synthetic ACL failure.' }
    $script:ProtectedPath=$Path; $script:Events += 'acl'
}
function Get-UVPNProfileWgExecutable($Path) { return 'C:\Program Files\WireGuard\wg.exe' }
function New-UVPNProfileKeys($Executable) {
    if (-not $script:ProtectedPath) { throw 'Keys generated before directory protection.' }
    $script:Events += 'keys'; $script:KeyCalls += 1
    if ($script:FailKeygen) { throw 'Synthetic key generation failure.' }
    return $script:Keys
}
function Invoke-UVPNGateway {
    param($Mode,$TunnelConfig,$GatewayConfig,$Endpoint)
    if ($Mode -ne 'Plan') { throw 'Profile invoked an installing mode.' }
    $script:Events += 'plan'; $script:PlanCalls += 1
    $checked=Read-UVPNValidatedConfig ([IO.File]::ReadAllText($TunnelConfig)) $GatewayConfig $Endpoint
    return [pscustomobject]@{Mode='Plan';ClientAddress=$checked.ClientAddress;Destinations=$checked.Destinations;Endpoint=$checked.Endpoint}
}
function Read-Host {
    param($Prompt)
    if (-not $script:Answers.Count) { throw 'An unexpected interactive question was asked.' }
    $answer=$script:Answers[0]
    $script:Answers=@($script:Answers | Select-Object -Skip 1)
    return $answer
}
function Assert($Condition,$Message) { if (-not $Condition) { throw ('Assertion: ' + $Message) } }
function Check($Name,[scriptblock]$Body) {
    $script:Events=@(); $script:KeyCalls=0; $script:PlanCalls=0; $script:ProtectedPath=$null
    $script:FailAcl=$false; $script:FailKeygen=$false
    $script:Answers=@()
    & $Body
    $script:Passed += 1
    Write-Output ('PASS ' + $Name)
}
function Reject([scriptblock]$Body) {
    $caught=$false
    try { & $Body | Out-Null } catch { $caught=$true }
    Assert $caught 'invalid profile accepted'
    Assert ($script:KeyCalls -eq 0 -and $script:PlanCalls -eq 0) 'invalid profile invoked keygen or Plan'
    Assert ($script:Events.Count -eq 0) 'invalid profile changed directory protection'
}

$temp=Join-Path ([IO.Path]::GetTempPath()) ('univpn-profile-test-' + [Guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $temp
$source=Join-Path $temp 'source.json'
$sourceText='{"gateway":"vpn.example.com","port":443,"allowed_cidrs":["10.80.0.11/32","10.80.0.12/32"],"wireguard_client":"10.203.99.2/32","probe":{"host":"10.80.0.11","port":22,"banner_prefix":"SSH-2.0-"}}'
[IO.File]::WriteAllText($source,$sourceText)
try {
    Check 'Init derives a paired profile and reuses the existing Plan' {
        $result=Invoke-UVPNProfile -Mode Init -Name lab -RootDirectory $temp -GatewayConfig $source `
            -AccessSubnet 10.203.1.0/30 -UdpPort 51830
        Assert ($script:Events -join ',' -eq 'acl,keys,plan') 'incorrect protection/keygen/plan order'
        Assert ($result.ProjectName -eq 'univpn-lab' -and $result.Image -eq 'javazero/univpn-docker-gateway:latest') 'wrong identity/image'
        Assert ($result.Endpoint -eq '127.0.0.1:51830' -and $result.UdpPort -eq 51830) 'endpoint mismatch'
        $vpn=[IO.File]::ReadAllText($result.GatewayConfig) | ConvertFrom-Json
        Assert ($vpn.wireguard_client -eq '10.203.1.2/32') 'wrong derived client'
        Assert ($vpn.allowed_cidrs -join ',' -eq '10.80.0.11/32,10.80.0.12/32') 'targets changed'
        $server=[IO.File]::ReadAllText($result.ServerConfig)
        $client=[IO.File]::ReadAllText($result.TunnelConfig)
        Assert ($server.Contains('Address = 10.203.1.1/30') -and $server.Contains('Table = off')) 'wrong server link'
        Assert ($server.Contains('PrivateKey = ' + $script:Keys.ServerPrivate) -and
            $server.Contains('PublicKey = ' + $script:Keys.ClientPublic)) 'server key pairing failed'
        Assert ($client.Contains('PrivateKey = ' + $script:Keys.ClientPrivate) -and
            $client.Contains('PublicKey = ' + $script:Keys.ServerPublic)) 'client key pairing failed'
        Assert ($client.Contains('Endpoint = 127.0.0.1:51830')) 'client endpoint mismatch'
        Assert (-not $server.Contains("`r") -and -not $client.Contains("`r")) 'WireGuard files need LF'
        Assert ([IO.File]::ReadAllText($source) -ceq $sourceText) 'source GatewayConfig changed'
        $serialized=$result | ConvertTo-Json -Depth 7
        $manifest=[IO.File]::ReadAllText((Join-Path $result.ProfileDirectory 'profile.json'))
        foreach ($key in $script:Keys.PSObject.Properties.Value) {
            Assert (-not $serialized.Contains($key) -and -not $manifest.Contains($key)) 'key exposed in result/manifest'
        }
        $meta=$manifest | ConvertFrom-Json
        Assert ($meta.udp_port -eq 51830 -and $meta.schema_version -eq 1) 'manifest mismatch'
        Assert (@($meta.PSObject.Properties.Name).Count -eq 5) 'manifest has unexpected data'
    }
    Check 'an existing profile is never overwritten or re-keyed' {
        $path=Join-Path $temp 'private/lab/wg0.conf'
        $before=[IO.File]::ReadAllText($path)
        Reject { Invoke-UVPNProfile -Mode Init -Name lab -RootDirectory $temp -GatewayConfig $source }
        Assert ([IO.File]::ReadAllText($path) -ceq $before) 'existing key file changed'
    }
    Check 'Plan does not create keys or write profile files' {
        $profile=Join-Path $temp 'private/lab'
        $before=@(Get-ChildItem -LiteralPath $profile | ForEach-Object { $_.Name + ':' + (Get-FileHash -LiteralPath $_.FullName).Hash })
        $result=Invoke-UVPNProfile -Mode Plan -Name lab -RootDirectory $temp
        $after=@(Get-ChildItem -LiteralPath $profile | ForEach-Object { $_.Name + ':' + (Get-FileHash -LiteralPath $_.FullName).Hash })
        Assert ($before -join ',' -ceq $after -join ',') 'Plan changed files'
        Assert ($script:KeyCalls -eq 0 -and $script:PlanCalls -eq 1) 'Plan generated keys or skipped existing validator'
        Assert ($result.Endpoint -eq '127.0.0.1:51830') 'Plan ignored stored profile endpoint'
    }
    Check 'parameters can create a profile without interactive questions' {
        $result=Invoke-UVPNProfile -Mode Init -Name params -RootDirectory $temp -Gateway vpn.example.com `
            -AllowedCidrs @('10.80.0.11/32') -ProbeHost 10.80.0.11 -UdpPort 51831
        Assert ($result.Plan.Destinations[0] -eq '10.80.0.11/32') 'parameter targets missing'
    }
    Check 'interactive setup asks endpoints and targets but no account or password' {
        $script:Answers=@('vpn.example.com','4443','10.80.0.11/32','')
        $result=Invoke-UVPNProfile -Mode Init -Name interactive -RootDirectory $temp -UdpPort 51832
        $config=[IO.File]::ReadAllText($result.GatewayConfig) | ConvertFrom-Json
        Assert ($config.port -eq 4443 -and $config.probe.host -eq '10.80.0.11') 'interactive endpoint not preserved'
        Assert ($script:Answers.Count -eq 0) 'interactive answers not consumed as expected'
        Assert (-not $config.PSObject.Properties['password'] -and -not $config.PSObject.Properties['username']) 'credentials in generated config'
    }
    Check 'interactive host colon port supports non-default VPN ports' {
        $script:Answers=@('vpn.example.com:41024','10.80.0.11/32','')
        $result=Invoke-UVPNProfile -Mode Init -Name inline -RootDirectory $temp -UdpPort 51833
        $config=[IO.File]::ReadAllText($result.GatewayConfig) | ConvertFrom-Json
        Assert ($config.gateway -eq 'vpn.example.com' -and $config.port -eq 41024) 'inline endpoint not split correctly'
        Assert ($script:Answers.Count -eq 0) 'inline endpoint asked an extra port question'
    }
    Check 'interactive bad ports and IPv6 are rejected before generating keys' {
        foreach ($answer in @('vpn.example.com:0','vpn.example.com:65536','vpn.example.com:abc','[::1]:443','999.999.1.1:443')) {
            $script:Answers=@($answer,'10.80.0.11/32','')
            Reject { Invoke-UVPNProfile -Mode Init -Name invalidport -RootDirectory $temp }
        }
    }
    Check 'explicit GatewayPort keeps its parameter semantics' {
        $script:Answers=@('vpn.example.com','10.80.0.11/32','')
        $result=Invoke-UVPNProfile -Mode Init -Name explicitport -RootDirectory $temp -GatewayPort 49999 -UdpPort 51834
        $config=[IO.File]::ReadAllText($result.GatewayConfig) | ConvertFrom-Json
        Assert ($config.port -eq 49999 -and $script:Answers.Count -eq 0) 'explicit gateway port was overridden or re-asked'
    }
    Check 'conflicting inline and explicit gateway ports are rejected' {
        $script:Answers=@('vpn.example.com:41024')
        Reject { Invoke-UVPNProfile -Mode Init -Name conflictport -RootDirectory $temp -GatewayPort 49999 }
    }
    Check 'interactive probe can use a non-standard SSH port' {
        $script:Answers=@('vpn.example.com:41024','10.80.0.11/32','10.80.0.11:2222')
        $result=Invoke-UVPNProfile -Mode Init -Name probeport -RootDirectory $temp -UdpPort 51835
        $config=[IO.File]::ReadAllText($result.GatewayConfig) | ConvertFrom-Json
        Assert ($config.probe.host -eq '10.80.0.11' -and $config.probe.port -eq 2222) 'probe endpoint not split correctly'
    }
    Check 'invalid interactive probe ports are rejected before keygen' {
        $script:Answers=@('vpn.example.com:41024','10.80.0.11/32','10.80.0.11:65536')
        Reject { Invoke-UVPNProfile -Mode Init -Name invalidprobe -RootDirectory $temp }
    }
    Check 'invalid names images ports and access networks fail before keygen' {
        foreach ($name in @('../escape','UPPER','a/b','univpn;run')) {
            Reject { Invoke-UVPNProfile -Mode Init -Name $name -RootDirectory $temp -GatewayConfig $source }
        }
        foreach ($image in @('image;run','x y','$(run)','UPPER/image:tag')) {
            Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $source -Image $image }
        }
        foreach ($port in @(0,65536)) {
            Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $source -UdpPort $port }
        }
        foreach ($subnet in @('0.0.0.0/0','10.203.0.1/30','10.203.0.0/24','203.0.113.0/30')) {
            Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $source -AccessSubnet $subnet }
        }
    }
    Check 'default targets overlap and credential fields are refused' {
        $bad=Join-Path $temp 'bad.json'
        foreach ($text in @($sourceText.Replace('10.80.0.11/32','0.0.0.0/0'),
                             $sourceText.Replace('"gateway":','"password":"do-not-print","gateway":'))) {
            [IO.File]::WriteAllText($bad,$text)
            Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $bad }
        }
        Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $source -AccessSubnet 10.80.0.8/30 }
    }
    Check 'outside probe and duplicate target fail before keygen' {
        $bad=Join-Path $temp 'bad.json'
        foreach ($text in @($sourceText.Replace('"host":"10.80.0.11"','"host":"10.90.0.1"'),
                             $sourceText.Replace('10.80.0.12/32','10.80.0.11/32'))) {
            [IO.File]::WriteAllText($bad,$text)
            Reject { Invoke-UVPNProfile -Mode Init -Name bad -RootDirectory $temp -GatewayConfig $bad }
        }
    }
    Check 'failed ACL protection prevents any key generation or files' {
        $script:FailAcl=$true; $caught=$false
        try { Invoke-UVPNProfile -Mode Init -Name aclfail -RootDirectory $temp -GatewayConfig $source | Out-Null }
        catch { $caught=$true }
        Assert ($caught -and $script:KeyCalls -eq 0 -and $script:PlanCalls -eq 0) 'ACL failure did not stop initialization'
        Assert (@(Get-ChildItem -LiteralPath (Join-Path $temp 'private/aclfail')).Count -eq 0) 'files were written before ACL protection'
    }
    Check 'keygen failure does not publish partial configuration or secret output' {
        $script:FailKeygen=$true; $message=$null
        try { Invoke-UVPNProfile -Mode Init -Name keyfail -RootDirectory $temp -GatewayConfig $source | Out-Null }
        catch { $message=$_.Exception.Message }
        Assert ($message -and $script:PlanCalls -eq 0) 'failed keygen reached Plan'
        Assert (@(Get-ChildItem -LiteralPath (Join-Path $temp 'private/keyfail')).Count -eq 0) 'partial configuration published'
        foreach ($key in $script:Keys.PSObject.Properties.Value) { Assert (-not $message.Contains($key)) 'secret in error' }
    }
    Write-Output ("All $script:Passed profile checks passed; only local synthetic fixtures were written.")
} finally {
    $resolved=[IO.Path]::GetFullPath($temp)
    $expectedBase=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolved.StartsWith($expectedBase,[StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolved).StartsWith('univpn-profile-test-')) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    } else { throw 'Refusing to clean an unexpected test directory.' }
}
