# Package Estimo for Azure App Service Deployment
$destination = "D:\KLE fest 2\app.zip"
if (Test-Path $destination) { Remove-Item $destination -Force }

$includeItems = @(
    "app.py",
    "wsgi.py",
    "run.py",
    "requirements.txt",
    "estimo",
    "ml",
    "pipeline_app"
)

# Temporary staging folder
$staging = "D:\KLE fest 2\_staging"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null

foreach ($item in $includeItems) {
    $src = "D:\KLE fest 2\$item"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $staging -Recurse -Force
    }
}

# Remove __pycache__ from staging
Get-ChildItem -Path $staging -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Create zip
Compress-Archive -Path "$staging\*" -DestinationPath $destination -Force
Remove-Item $staging -Recurse -Force

Write-Output "Successfully created $destination for Azure App Service deployment!"
