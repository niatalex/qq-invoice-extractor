$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath 'vendor\7z.exe') -or !(Test-Path -LiteralPath 'vendor\7z.dll')) {
    $seven = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
    if (!(Test-Path -LiteralPath $seven)) { throw 'Install official 7-Zip first: https://www.7-zip.org/' }
    New-Item -ItemType Directory -Force -Path 'vendor' | Out-Null
    Copy-Item -LiteralPath $seven -Destination 'vendor\7z.exe' -Force
    Copy-Item -LiteralPath (Join-Path (Split-Path $seven) '7z.dll') -Destination 'vendor\7z.dll' -Force
    foreach ($license in @('License.txt','readme.txt')) {
        $source = Join-Path (Split-Path $seven) $license
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination 'vendor' -Force }
    }
}
& '.venv\Scripts\python.exe' -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& '.venv\Scripts\python.exe' build_release.py --prepare
if ($LASTEXITCODE -ne 0) { throw 'Version resource generation failed' }
& '.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --windowed --name 'InvoiceExtractor' --version-file 'version_info.txt' --add-data 'vendor/7z.exe;vendor' --add-data 'vendor/7z.dll;vendor' --add-data 'vendor/License.txt;vendor' app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
$process = Start-Process -FilePath (Join-Path $PSScriptRoot 'dist\InvoiceExtractor\InvoiceExtractor.exe') -ArgumentList '--smoke-test' -WindowStyle Hidden -PassThru
if (!$process.WaitForExit(30000)) { $process.Kill(); throw 'Packaged self-test timed out' }
$process.Refresh()
if ($process.ExitCode -ne 0) { throw 'Packaged self-test failed' }
& '.venv\Scripts\python.exe' build_release.py --package
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }