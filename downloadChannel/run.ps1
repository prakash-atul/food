# Convenience runner (Windows PowerShell). Reads .\list.txt, .\cookies.txt -> .\output\
# Override the endpoint by setting $env:VLLM_BASE_URL before running.
if (-not $env:VLLM_BASE_URL) { $env:VLLM_BASE_URL = "http://localhost:8005/v1" }
if (-not $env:REQUEST_TIMEOUT) { $env:REQUEST_TIMEOUT = "120" }
if (-not $env:RETRIES) { $env:RETRIES = "4" }
python app.py @args
