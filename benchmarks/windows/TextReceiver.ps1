param(
    [string]$Title = "Scriber Autoresearch TextReceiver",
    [string]$AutomationId = "ScriberAutoresearchTextBox"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 900
$form.Height = 420
$form.StartPosition = "CenterScreen"
$form.TopMost = $true

$textbox = New-Object System.Windows.Forms.TextBox
$textbox.Multiline = $true
$textbox.AcceptsReturn = $true
$textbox.AcceptsTab = $true
$textbox.ScrollBars = "Vertical"
$textbox.Dock = "Fill"
$textbox.Font = New-Object System.Drawing.Font("Consolas", 12)
$textbox.Name = $AutomationId
$textbox.AccessibleName = $AutomationId

$form.Controls.Add($textbox)
$form.Add_Shown({
    $form.Activate()
    $textbox.Focus()
})

[System.Windows.Forms.Application]::Run($form)
