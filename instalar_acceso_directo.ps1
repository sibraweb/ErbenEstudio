# Crea el acceso directo de ERBEN ESTUDIO en el Escritorio.
# Correr una sola vez:  powershell -ExecutionPolicy Bypass -File instalar_acceso_directo.ps1

$aqui      = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat       = Join-Path $aqui "ERBEN.bat"
$escritorio = [Environment]::GetFolderPath("Desktop")
$lnk       = Join-Path $escritorio "ERBEN ESTUDIO.lnk"

if (-not (Test-Path $bat)) { Write-Host "  No encuentro ERBEN.bat" -ForegroundColor Red; exit 1 }

# Un ícono propio: se genera una vez y queda al lado del .bat.
$ico = Join-Path $aqui "erben.ico"
if (-not (Test-Path $ico)) {
  Add-Type -AssemblyName System.Drawing
  $bmp = New-Object System.Drawing.Bitmap 256,256
  $g   = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  # el mismo verde de la app, con las iniciales
  $fondo = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    (New-Object System.Drawing.Point 0,0), (New-Object System.Drawing.Point 256,256),
    [System.Drawing.Color]::FromArgb(14,166,120), [System.Drawing.Color]::FromArgb(11,127,92))
  $g.FillRectangle($fondo, 0, 0, 256, 256)
  $fuente = New-Object System.Drawing.Font("Segoe UI", 104, [System.Drawing.FontStyle]::Bold)
  $fmt = New-Object System.Drawing.StringFormat
  $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
  $g.DrawString("EE", $fuente, [System.Drawing.Brushes]::White,
                (New-Object System.Drawing.RectangleF 0,0,256,256), $fmt)
  $g.Dispose()

  $hIcon = $bmp.GetHicon()
  $icono = [System.Drawing.Icon]::FromHandle($hIcon)
  $fs = [System.IO.File]::Create($ico)
  $icono.Save($fs)
  $fs.Close(); $icono.Dispose(); $bmp.Dispose()
  Write-Host "  Icono creado: $ico"
}

$sh = New-Object -ComObject WScript.Shell
$acceso = $sh.CreateShortcut($lnk)
$acceso.TargetPath       = $bat
$acceso.WorkingDirectory = $aqui
$acceso.IconLocation     = "$ico,0"
$acceso.Description      = "ERBEN ESTUDIO - sistema contable"
$acceso.Save()

Write-Host ""
Write-Host "  Listo: 'ERBEN ESTUDIO' esta en el Escritorio." -ForegroundColor Green
Write-Host "  Doble clic y el sistema se abre solo en el navegador."
Write-Host ""
