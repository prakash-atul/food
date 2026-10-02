# Open (and auto-reconnect) the SSH tunnel to the HPC Qwen endpoint (Windows PowerShell).
# Keep this running in its own terminal while app.py downloads.
#
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Port 8010
#
# The network sometimes drops; this loop re-establishes the tunnel within ~3s.

param(
  [int]$Port = 8005,
  [string]$Hpc = "atul_prakash@10.1.7.58"
)

Write-Host "Tunneling localhost:$Port -> ${Hpc}:localhost:$Port  (Ctrl-C to stop)"
while ($true) {
  ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 `
      -o ConnectTimeout=10 -N -L "${Port}:localhost:$Port" $Hpc
  Write-Host "[tunnel] dropped, reconnecting in 3s..."
  Start-Sleep -Seconds 3
}
