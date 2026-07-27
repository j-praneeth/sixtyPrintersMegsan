<#
    Print & Register  (batch)
    =========================
    Run by the NORMAL user (no elevation). Lets you pick one or more files, give
    each its OWN registration number, and print them to the virtual printer one at
    a time - writing each number just before its file prints and waiting until the
    print pipeline consumes it before moving on. That guarantees every PDF carries
    a different registration number, even for batches and with many users.

    Mechanism: writes %ProgramData%\VirtualCloudPrinter\ids\<user>.id = {id, once:true};
    upload.py (SYSTEM) reads+consumes it for the very next job. Same file/contract
    as set-id.ps1 - so the single-print "Set Print ID" dialog still works too.

    Launched by print-register.bat.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Base   = Join-Path $env:ProgramData 'VirtualCloudPrinter'
$IdsDir  = Join-Path $Base 'ids'
$safe    = ($env:USERNAME -replace '[^A-Za-z0-9._-]', '_')   # MUST match upload.py:sanitize_userfile
$IdFile  = Join-Path $IdsDir ($safe + '.id')
$PortName = 'VirtualCloudPrinter:'
$ConsumeTimeoutSec = 90

function Show-Msg($text, $icon = 'Information') {
    [System.Windows.Forms.MessageBox]::Show($text, 'Print & Register', 'OK', $icon) | Out-Null
}

if (-not (Test-Path $IdsDir)) {
    Show-Msg "Virtual Cloud Printer is not installed yet (no ids folder).`nRun install.bat first." 'Warning'
    return
}

# --- which printer? any printer sitting on our redirection port ---
# Get-Printer requires Win8+; on Windows 7 enumerate via WMI instead.
$printers = if (Get-Command Get-Printer -ErrorAction SilentlyContinue) {
    @(Get-Printer -ErrorAction SilentlyContinue | Where-Object { $_.PortName -eq $PortName } | Select-Object -ExpandProperty Name)
} else {
    @(Get-WmiObject -Class Win32_Printer -ErrorAction SilentlyContinue | Where-Object { $_.PortName -eq $PortName } | Select-Object -ExpandProperty Name)
}
if ($printers.Count -eq 0) {
    Show-Msg "No virtual printer found (none on port $PortName).`nRun install.bat first." 'Warning'
    return
}

# --- pick files ---
$ofd = New-Object System.Windows.Forms.OpenFileDialog
$ofd.Multiselect = $true
$ofd.Title = 'Select the file(s) to print & register'
$ofd.Filter = 'Printable files (*.pdf;*.doc;*.docx;*.txt;*.jpg;*.png)|*.pdf;*.doc;*.docx;*.txt;*.jpg;*.png|All files (*.*)|*.*'
if ($ofd.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }
$files = $ofd.FileNames

# --- build the form: printer picker + grid of (file, registration #) ---
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Print & Register'
$form.Size = New-Object System.Drawing.Size(680, 460)
$form.StartPosition = 'CenterScreen'
$form.MinimumSize = New-Object System.Drawing.Size(560, 360)

$lblP = New-Object System.Windows.Forms.Label
$lblP.Text = 'Printer:'; $lblP.Location = '12,15'; $lblP.AutoSize = $true
$form.Controls.Add($lblP)

$cbo = New-Object System.Windows.Forms.ComboBox
$cbo.Location = '70,12'; $cbo.Size = New-Object System.Drawing.Size(260, 24)
$cbo.DropDownStyle = 'DropDownList'
$printers | ForEach-Object { [void]$cbo.Items.Add($_) }
$cbo.SelectedIndex = 0
$form.Controls.Add($cbo)

$grid = New-Object System.Windows.Forms.DataGridView
$grid.Location = '12,48'; $grid.Size = New-Object System.Drawing.Size(640, 300)
$grid.Anchor = 'Top,Bottom,Left,Right'
$grid.AllowUserToAddRows = $false
$grid.RowHeadersVisible = $false
$grid.AutoSizeColumnsMode = 'Fill'
$colFile = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colFile.HeaderText = 'File'; $colFile.ReadOnly = $true; $colFile.FillWeight = 55
$colReg = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colReg.HeaderText = 'Registration number'; $colReg.FillWeight = 30
$colStat = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colStat.HeaderText = 'Status'; $colStat.ReadOnly = $true; $colStat.FillWeight = 15
[void]$grid.Columns.Add($colFile); [void]$grid.Columns.Add($colReg); [void]$grid.Columns.Add($colStat)
foreach ($f in $files) { [void]$grid.Rows.Add((Split-Path $f -Leaf), '', '') }
$form.Controls.Add($grid)

# auto-fill: prefix + incrementing number
$lblPre = New-Object System.Windows.Forms.Label
$lblPre.Text = 'Auto-fill  prefix:'; $lblPre.Location = '12,360'; $lblPre.AutoSize = $true
$form.Controls.Add($lblPre)
$txtPre = New-Object System.Windows.Forms.TextBox
$txtPre.Location = '110,357'; $txtPre.Size = New-Object System.Drawing.Size(90, 24); $txtPre.Text = 'REG-'
$form.Controls.Add($txtPre)
$lblStart = New-Object System.Windows.Forms.Label
$lblStart.Text = 'start:'; $lblStart.Location = '210,360'; $lblStart.AutoSize = $true
$form.Controls.Add($lblStart)
$numStart = New-Object System.Windows.Forms.NumericUpDown
$numStart.Location = '250,357'; $numStart.Size = New-Object System.Drawing.Size(70, 24)
$numStart.Minimum = 0; $numStart.Maximum = 999999999; $numStart.Value = 1
$form.Controls.Add($numStart)
$btnFill = New-Object System.Windows.Forms.Button
$btnFill.Text = 'Fill'; $btnFill.Location = '328,356'; $btnFill.Size = New-Object System.Drawing.Size(60, 25)
$btnFill.Add_Click({
    $n = [int]$numStart.Value
    foreach ($row in $grid.Rows) { $row.Cells[1].Value = ('{0}{1}' -f $txtPre.Text, $n); $n++ }
})
$form.Controls.Add($btnFill)

$btnPrint = New-Object System.Windows.Forms.Button
$btnPrint.Text = 'Print all'; $btnPrint.Location = '470,356'; $btnPrint.Size = New-Object System.Drawing.Size(90, 28)
$btnPrint.Anchor = 'Bottom,Right'
$form.Controls.Add($btnPrint)
$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = 'Close'; $btnClose.Location = '565,356'; $btnClose.Size = New-Object System.Drawing.Size(85, 28)
$btnClose.Anchor = 'Bottom,Right'
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

function Write-Id($value) {
    $json = (@{ id = $value; once = $true } | ConvertTo-Json -Compress)
    [System.IO.File]::WriteAllText($IdFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$btnPrint.Add_Click({
    $printer = $cbo.SelectedItem
    $btnPrint.Enabled = $false; $btnClose.Enabled = $false
    try {
        for ($i = 0; $i -lt $grid.Rows.Count; $i++) {
            $row = $grid.Rows[$i]
            $reg = ('' + $row.Cells[1].Value).Trim()
            $file = $files[$i]
            if (-not $reg) { $row.Cells[2].Value = 'skipped (no #)'; continue }

            # Clear any stale id, write this job's number, then print this one file.
            if (Test-Path $IdFile) { Remove-Item $IdFile -Force -ErrorAction SilentlyContinue }
            Write-Id $reg
            $row.Cells[2].Value = 'printing...'
            $grid.Refresh()
            try {
                Start-Process -FilePath $file -Verb PrintTo -ArgumentList "`"$printer`"" -ErrorAction Stop | Out-Null
            } catch {
                $row.Cells[2].Value = 'print failed'
                Remove-Item $IdFile -Force -ErrorAction SilentlyContinue
                continue
            }

            # Wait until upload.py consumes the id (file disappears) => this job
            # has claimed its number, so it is safe to print the next file.
            $deadline = (Get-Date).AddSeconds($ConsumeTimeoutSec)
            while ((Test-Path $IdFile) -and ((Get-Date) -lt $deadline)) {
                Start-Sleep -Milliseconds 300
                [System.Windows.Forms.Application]::DoEvents()
            }
            if (Test-Path $IdFile) {
                $row.Cells[2].Value = 'timeout (not picked up)'
                Remove-Item $IdFile -Force -ErrorAction SilentlyContinue
            } else {
                $row.Cells[2].Value = 'sent: ' + $reg
            }
        }
        Show-Msg 'Done. Check the receiver dashboard for the documents and their registration numbers.'
    } finally {
        $btnPrint.Enabled = $true; $btnClose.Enabled = $true
    }
})

$form.Topmost = $true
[void]$form.ShowDialog()
