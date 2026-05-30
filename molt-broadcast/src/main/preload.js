'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  createRoom: () => ipcRenderer.invoke('create-room'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window'),
  getSources: () => ipcRenderer.invoke('get-sources'),
});
