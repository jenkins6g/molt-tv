'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  createRoom: () => ipcRenderer.invoke('create-room'),
  getSources: () => ipcRenderer.invoke('get-sources'),
});
