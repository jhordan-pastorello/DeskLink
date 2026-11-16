# Chamado pelo instalador (como administrador). Registra a tarefa de logon do DeskLink.
param(
    [Parameter(Mandatory)][string]$Exe,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$name = 'DeskLink'

Get-Process DeskLink -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
if ($Remove) { return }

$action = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory (Split-Path $Exe)
$trigger = New-ScheduledTaskTrigger -AtLogOn
# rede de seguranca: reexecuta a cada 5 min (IgnoreNew evita duplicar)
try {
    $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $trigger.Repetition = $rep.Repetition
} catch { Write-Warning "repeticao nao configurada: $_" }

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
# Qualquer usuario local que fizer logon; roda na sessao interativa (unico lugar onde os hooks funcionam)
$principal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\Users' -RunLevel Highest

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
