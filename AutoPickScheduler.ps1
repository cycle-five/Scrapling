# AutoPick Randomized Scheduler
# Runs docker command every 75 minutes +/- 15 minutes (60-90 minute range)

# Function to get home directory (cross-platform)
function Get-HomeDirectory {
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        return $env:USERPROFILE
    } else {
        return $env:HOME
    }
}

# Configuration
$homeDir = Get-HomeDirectory
$workDir = Join-Path $homeDir "AutoPick"
$envFile = Join-Path $homeDir "picks.env"
$logFile = Join-Path $workDir "autopick_scheduler.log"

# Ensure work directory exists
if (-not (Test-Path $workDir)) {
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
}

# Function to write log with timestamp
function Write-Log {
    param($Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    Write-Host $logMessage
    Add-Content -Path $logFile -Value $logMessage
}

# Function to get random delay in seconds (60-90 minutes)
function Get-RandomDelay {
    $minMinutes = 60
    $maxMinutes = 90
    $randomMinutes = Get-Random -Minimum $minMinutes -Maximum ($maxMinutes + 1)
    return $randomMinutes * 60
}

Write-Log "AutoPick Scheduler Started"
Write-Log "Work Directory: $workDir"
Write-Log "Env File: $envFile"

# Main loop
while ($true) {
    try {
        Write-Log "Running AutoPick docker container..."
        
        # Change to work directory
        Set-Location $workDir
        
        # Run the docker command
        $dockerCmd = "docker run -v ./screenshots:/app/screenshots -v ./picks_data:/app/picks_data --env-file=$envFile cyclefive/auto-pick:dev --headless --summarize"
        
        Write-Log "Executing: $dockerCmd"
        Invoke-Expression $dockerCmd
        
        $exitCode = $LASTEXITCODE
        Write-Log "Docker command completed with exit code: $exitCode"
        
        # Calculate next run time
        $delaySeconds = Get-RandomDelay
        $delayMinutes = [math]::Round($delaySeconds / 60, 1)
        $nextRun = (Get-Date).AddSeconds($delaySeconds)
        
        Write-Log "Waiting $delayMinutes minutes until next run (at $($nextRun.ToString('yyyy-MM-dd HH:mm:ss')))"
        
        # Sleep for the random delay
        Start-Sleep -Seconds $delaySeconds
        
    }
    catch {
        Write-Log "ERROR: $($_.Exception.Message)"
        Write-Log "Waiting 5 minutes before retry..."
        Start-Sleep -Seconds 300
    }
}