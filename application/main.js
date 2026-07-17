// main.js
// Main process: manages app lifecycle, spawns and supervises the Python
// backend as a child process, owns the local WebSocket connection to
// it, and creates the renderer window. See implementation.md Section 8.1
// for the reasoning behind this process split.

const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const WebSocket = require('ws');
const path = require('path');

const BACKEND_PORT = 8765;
const BACKEND_CONNECT_DELAY_MS = 1500;
const MAX_RESTART_ATTEMPTS = 5;

let pythonProcess = null;
let backendSocket = null;
let mainWindow = null;
let restartAttempts = 0;
let intentionalShutdown = false;

function pythonExecutable() {
  // Assumes a virtualenv/conda env set up per README has `python` on
  // PATH with pipeline/requirements.txt installed. Override via
  // VA_PYTHON_BIN if the target machine needs a specific interpreter
  // (e.g. a bundled PyInstaller binary in packaged builds -- see
  // implementation.md Section 8.7).
  return process.env.VA_PYTHON_BIN || 'python';
}

// Guards every renderer send against a destroyed window/webContents.
// Without this, any send that races against the window closing (e.g. the
// backend's 'exit' event firing right as the user closes the app, or during
// shutdown) throws an uncaught "Object has been destroyed" TypeError in the
// main process, which Electron surfaces as a hard error dialog.
function safeSend(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents && !mainWindow.webContents.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function startPythonBackend() {
  // Run as `-m pipeline.run_pipeline` (not as a bare script path) so that
  // the `pipeline` package's internal imports (`from pipeline.config import
  // config`, etc.) resolve correctly -- this requires the project root
  // (the parent of the `pipeline/` folder) to be on sys.path, which only
  // happens when Python is invoked with `-m` from that root directory.
  const projectRoot = path.join(__dirname, '..');
  pythonProcess = spawn(pythonExecutable(), ['-m', 'pipeline.run_pipeline'], {
    stdio: ['ignore', 'pipe', 'pipe'],
    cwd: projectRoot,
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[backend stdout] ${data}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[backend stderr] ${data}`);
  });

  pythonProcess.on('exit', (code, signal) => {
    // `code` is null when the process was terminated by a signal rather
    // than exiting normally (e.g. killed by the OS, a crash outside
    // Python's own exception handling, or `pythonProcess.kill()` during
    // shutdown) -- logging `signal` too makes that distinction visible
    // instead of just printing a mysterious "code null".
    console.error(`Python backend exited with code ${code} (signal: ${signal})`);
    safeSend('backend-status', { status: 'crashed', code, signal });
    if (backendSocket) {
      backendSocket.removeAllListeners();
      backendSocket = null;
    }
    if (!intentionalShutdown && restartAttempts < MAX_RESTART_ATTEMPTS) {
      restartAttempts += 1;
      const backoffMs = Math.min(1000 * 2 ** restartAttempts, 15000);
      console.log(`Restarting backend in ${backoffMs}ms (attempt ${restartAttempts}/${MAX_RESTART_ATTEMPTS})`);
      setTimeout(startPythonBackend, backoffMs);
    } else if (!intentionalShutdown) {
      safeSend('backend-status', { status: 'failed-permanently' });
    }
  });

  setTimeout(connectToBackend, BACKEND_CONNECT_DELAY_MS);
}

function connectToBackend() {
  backendSocket = new WebSocket(`ws://localhost:${BACKEND_PORT}`);

  backendSocket.on('open', () => {
    restartAttempts = 0;
    safeSend('backend-status', { status: 'connected' });
  });

  backendSocket.on('message', (raw) => {
    let message;
    try {
      message = JSON.parse(raw.toString());
    } catch (err) {
      console.error('Malformed backend message:', raw.toString());
      return;
    }
    safeSend('backend-message', message);
  });

  backendSocket.on('close', () => {
    safeSend('backend-status', { status: 'disconnected' });
    if (!intentionalShutdown && pythonProcess && !pythonProcess.killed) {
      setTimeout(connectToBackend, 1000);
    }
  });

  backendSocket.on('error', (err) => {
    console.error('Backend socket error:', err.message);
  });
}

function sendControlMessage(message) {
  if (backendSocket && backendSocket.readyState === WebSocket.OPEN) {
    backendSocket.send(JSON.stringify(message));
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 480,
    height: 760,
    minWidth: 400,
    minHeight: 600,
    backgroundColor: '#242F40', // Jet Black -- avoids a white flash on load
    titleBarStyle: 'hiddenInset',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  // index.html/app.js/styles.css live alongside main.js in this project's
  // application/ folder (the main + renderer files are not split into
  // separate top-level directories here), so this is a same-directory load.
  mainWindow.loadFile(path.join(__dirname, 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(() => {
  createWindow();
  startPythonBackend();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  intentionalShutdown = true;
  if (pythonProcess) pythonProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  intentionalShutdown = true;
  if (pythonProcess) pythonProcess.kill();
});

ipcMain.on('control-message', (_event, message) => {
  sendControlMessage(message);
});

// Last-resort safety net: log instead of letting an unexpected main-process
// error surface as Electron's generic "A JavaScript error occurred in the
// main process" dialog. Anything caught here is still a bug worth fixing,
// but this keeps the app usable in the meantime rather than hard-crashing it.
process.on('uncaughtException', (err) => {
  console.error('Uncaught exception in main process:', err);
});