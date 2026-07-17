// preload.js
// Safe bridge between the isolated renderer and the main process.
// Exposes only the specific channels the UI needs -- no direct
// ipcRenderer or Node access leaks into the renderer's global scope.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('assistantBridge', {
  onBackendMessage: (callback) => {
    ipcRenderer.on('backend-message', (_event, message) => callback(message));
  },
  onBackendStatus: (callback) => {
    ipcRenderer.on('backend-status', (_event, status) => callback(status));
  },
  sendControl: (message) => {
    ipcRenderer.send('control-message', message);
  },
});