import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import os from 'node:os';

const execFileAsync = promisify(execFile);

export const openAppCommands = {
  calculator: ['calc.exe'],
  notepad: ['notepad.exe'],
  explorer: ['explorer.exe'],
  task_manager: ['taskmgr.exe'],
  control_panel: ['control.exe'],
  browser: ['explorer.exe', 'https://www.google.com']
};

export const runCommandActions = {
  ipconfig: ['ipconfig.exe', ['/all']],
  tasklist: ['tasklist.exe', ['/V']],
  systeminfo: ['systeminfo.exe', []]
};

function execute(file, args = []) {
  return execFileAsync(file, args, { windowsHide: true, maxBuffer: 1024 * 1024 });
}

export async function getStatus() {
  const [freeMemory, totalMemory] = [os.freemem(), os.totalmem()];
  const script = [
    '$uptime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime',
    '$boot = [datetime]$uptime',
    '$disk = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Select-Object DeviceID,FreeSpace,Size',
    '$diskJson = $disk | ConvertTo-Json -Compress',
    "[pscustomobject]@{uptimeMinutes=[math]::Round(((Get-Date)-$boot).TotalMinutes);disk=$diskJson} | ConvertTo-Json -Compress"
  ].join('; ');

  let disk = '';
  let uptimeMinutes = 0;
  try {
    const { stdout } = await execute('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      script
    ]);
    const data = JSON.parse(stdout);
    uptimeMinutes = data.uptimeMinutes;
    const disks = Array.isArray(data.disk) ? data.disk : JSON.parse(data.disk);
    disk = disks.map((item) =>
      `${item.DeviceID} ${Math.round(item.FreeSpace / 1073741824)} GB free / ${Math.round(item.Size / 1073741824)} GB`
    ).join('\n');
  } catch {
    uptimeMinutes = Math.round((Date.now() - (process.uptime() * 1000)) / 60000);
  }

  return [
    `PC: ${os.hostname()}`,
    `OS: ${os.platform()} ${os.release()} (${os.arch()})`,
    `Memory: ${Math.round((totalMemory - freeMemory) / 1073741824)} GB used / ${Math.round(totalMemory / 1073741824)} GB`,
    `Uptime: ${Math.floor(uptimeMinutes / 60)} h ${uptimeMinutes % 60} min`,
    disk ? `Disk:\n${disk}` : ''
  ].filter(Boolean).join('\n');
}

export function lockComputer() {
  return execute('rundll32.exe', ['user32.dll,LockWorkStation']);
}

export function openApp(name) {
  const command = openAppCommands[name];
  if (!command) throw new Error('App is not in the allowlist');
  return execute(command[0], command.slice(1));
}

export function runAllowedCommand(name) {
  const command = runCommandActions[name];
  if (!command) throw new Error('Command is not in the allowlist');
  return execute(command[0], command[1]);
}

export async function takeScreenshot(outputPath) {
  const script = [
    'Add-Type -AssemblyName System.Windows.Forms',
    'Add-Type -AssemblyName System.Drawing',
    '$b = New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height)',
    '$g = [System.Drawing.Graphics]::FromImage($b)',
    '$g.CopyFromScreen((New-Object System.Drawing.Point(0,0)),(New-Object System.Drawing.Point(0,0)), $b.Size)',
    `$b.Save('${outputPath.replaceAll("'", "''")}', [System.Drawing.Imaging.ImageFormat]::Png)`,
    '$g.Dispose(); $b.Dispose()'
  ].join('; ');
  await execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
}

export function sendMediaKey(action) {
  const codes = {
    prev: 0xb1,
    play_pause: 0xb3,
    next: 0xb0
  };
  const code = codes[action];
  if (!code) throw new Error('Unknown media action');

  const script = `
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class MediaKey {
  [DllImport("user32.dll")]
  public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
'@
[MediaKey]::keybd_event(${code}, 0, 0, [UIntPtr]::Zero)
[MediaKey]::keybd_event(${code}, 0, 2, [UIntPtr]::Zero)
`;
  return execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
}

export function sendVolumeKey(action) {
  const codes = { mute: 0xad, volume_down: 0xae, volume_up: 0xaf };
  const code = codes[action];
  if (!code) throw new Error('Unknown volume action');

  const script = `
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class VolumeKey {
  [DllImport("user32.dll")]
  public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
'@
[VolumeKey]::keybd_event(${code}, 0, 0, [UIntPtr]::Zero)
[VolumeKey]::keybd_event(${code}, 0, 2, [UIntPtr]::Zero)
`;
  return execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
}

export function powerAction(action) {
  const actions = {
    sleep: ['rundll32.exe', ['powrprof.dll,SetSuspendState', '0', '1', '0']],
    shutdown: ['shutdown.exe', ['/s', '/t', '10']],
    restart: ['shutdown.exe', ['/r', '/t', '10']]
  };
  const command = actions[action];
  if (!command) throw new Error('Unknown power action');
  return execute(command[0], command[1]);
}
