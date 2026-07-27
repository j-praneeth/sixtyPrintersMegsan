# Windows 7 prerequisites for `install_7.bat`

`install_7.bat` refuses to run until a Windows 7 SP1 machine has the two things
`setup.ps1` genuinely needs. This is **not a bug** — a stock Windows 7 SP1 install
usually ships with only .NET ~4.5 and **PowerShell 2.0**, both too old:

| Requirement | Why it's needed | `install_7.bat` needs |
|---|---|---|
| **.NET Framework 4.8** (4.7.2 minimum) | The super-admin password hash in `setup.ps1` uses the `Rfc2898DeriveBytes` SHA-256 constructor that only exists in .NET **4.7.2+**. On older .NET the very first install fails. | registry release ID **≥ 461808** |
| **WMF 5.1** (Windows PowerShell 5.1) | PowerShell 2.0 lacks `ConvertFrom-Json` / `Invoke-WebRequest` and can't even parse `setup.ps1`. The whole toolkit assumes PowerShell 5.1. | `$PSVersionTable.PSVersion.Major` **≥ 5** |

When you double-click `install_7.bat` it now prints what it detected, e.g.:

```
Detected: .NET Framework release ID = 0  (need 461808+ for 4.7.2)
Detected: PowerShell major version = 2  (need 5+)
```

Use those two lines to see which prerequisite is missing, then follow the matching
section below.

> **x64 only.** This toolkit targets Windows 7 SP1 **x64**. Confirm SP1 is installed:
> Start → right-click **Computer** → **Properties** → it should say
> *"Windows 7 ... Service Pack 1"* and *"64-bit Operating System"*. If SP1 is
> missing, install it via Windows Update first.

---

## Order matters

Install in **this exact order**, rebooting at the end:

1. **.NET Framework 4.8** — install first.
2. **WMF 5.1** (PowerShell 5.1) — install second (it wants .NET 4.5.2+ already present).
3. **Reboot.**
4. Run `install_7.bat` again.

---

## 1. Install .NET Framework 4.8

*(Skip if `install_7.bat` already reports a .NET release ID ≥ 461808.)*

1. Download the **.NET Framework 4.8 offline installer**:
   <https://dotnet.microsoft.com/download/dotnet-framework/net48>
   → click **Download .NET Framework 4.8 Runtime** (the offline installer,
   `ndp48-x86-x64-allos-enu.exe`, ~120 MB). The offline installer is best for the
   60 client PCs because it needs no internet during install.
2. Right-click the downloaded `.exe` → **Run as administrator** → accept the
   license → **Install**.
3. If it says *"…or a later version is already installed,"* you're done — .NET is fine.
4. **Reboot** if it asks (it usually does).

**Verify** (optional) — open Command Prompt and run:

```bat
reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" /v Release
```

The `Release` value should be **≥ 461808** (`0x82388` or higher). .NET 4.8 shows
`528040` or higher (`0x81368`+).

---

## 2. Install WMF 5.1 (Windows PowerShell 5.1)

*(Skip if `install_7.bat` already reports PowerShell major version ≥ 5.)*

1. Download **WMF 5.1** from the Microsoft Download Center:
   <https://www.microsoft.com/download/details.aspx?id=54616>
2. Pick the Windows 7 x64 package: **`Win7AndW2K8R2-KB3191566-x64.zip`**.
3. Right-click the `.zip` → **Extract All…**. Inside you'll find `Install-WMF5.1.ps1`
   and a `.msu` update.
4. Easiest install: double-click the extracted **`Win7AndW2K8R2-KB3191566-x64.msu`**
   → approve the update → let it finish.
   *(Alternatively, from an elevated PowerShell run `.\Install-WMF5.1.ps1`.)*
5. **Reboot** when prompted.

**Verify** (optional) — open a **new** PowerShell window and run:

```powershell
$PSVersionTable.PSVersion
```

The **Major** column should now read **5** (e.g. `5 1 ...`).

---

## 3. Re-run the installer

After both are installed and the machine has rebooted, double-click
**`install_7.bat`** again. You should now see:

```
Prerequisites OK (.NET Release 528040, PowerShell 5)
```

…and the click-through wizard opens. From there it's the same flow as Windows
10/11: hub URL, enroll key, printer name, device name/type, optional PDF password.
`setup.ps1` automatically uses the Windows 7 internals (Python 3.8 embeddable +
WMI printer management).

---

## Offline / mass rollout tips (60 machines)

- **Stage the installers once.** Download `ndp48-x86-x64-allos-enu.exe` and
  `Win7AndW2K8R2-KB3191566-x64.zip` on one machine and copy them to each Win 7 PC
  (USB / network share). Neither needs internet to install.
- **Stage the toolkit's own dependencies too.** On any online PC run once:

  ```
  powershell -ExecutionPolicy Bypass -File vendor\fetch-win7-bundle.ps1
  ```

  Then copy the whole repo folder (with `vendor\`) to each Win 7 client so
  `install_7.bat` installs fully offline (Python 3.8 embeddable, Ghostscript
  9.56.1, qpdf 10.6.3, mfilemon).
- **Reboot after WMF 5.1** — PowerShell 5.1 is not active until you do, and
  `install_7.bat`'s check will keep reporting version 2 until then.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `.NET Framework release ID = 0` | The reg key is missing/blocked, or no .NET 4.5+ is installed. Install .NET 4.8 (§1). If you're sure 4.8 is installed, re-run `install_7.bat` **as Administrator**. |
| `.NET release ID` is non-zero but `< 461808` | You have .NET 4.5/4.6/4.7 — below 4.7.2. Install .NET 4.8 (§1). |
| `PowerShell major version = 2` (or blank) | WMF 5.1 not installed, or installed but not rebooted. Do §2, then **reboot**. |
| WMF 5.1 `.msu` says *"update not applicable"* | Windows 7 **SP1** is required (and x64 package on x64 Windows). Install SP1 via Windows Update, then retry. |
| Says *"This machine is running Windows 10/11"* | You ran `install_7.bat` on a 10/11 box — use `install_10_11.bat` instead. |

See `SETUP.md` → *"Windows 7 SP1 clients"* for the full deployment context.
