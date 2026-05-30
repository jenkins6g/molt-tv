'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { app, BrowserWindow, ipcMain, session, desktopCapturer } = require('electron');
const path = require('path');
const { createDailyRoom } = require('./daily-api');

// Required for Electron's desktopCapturer to work with getUserMedia in renderer
app.commandLine.appendSwitch('enable-features', 'ElectronDesktopCapturer');

let mainWindow = null;

function createWindow() {
  // Allow media permissions so renderer can call getUserMedia with chromeMediaSource
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    callback(permission === 'media');
  });

  session.defaultSession.setPermissionCheckHandler((webContents, permission, requestingOrigin, details) => {
    if (permission === 'media') return true;
    return false;
  });

  const win = new BrowserWindow({
    width: 960,
    height: 720,
    title: 'molt-tv',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow = win;
  win.loadFile(path.join(__dirname, '../renderer/index.html'));

  if (process.env.NODE_ENV === 'development') {
    win.webContents.openDevTools();
  }
}

ipcMain.handle('create-room', async () => {
  const apiKey = process.env.DAILY_API_KEY;
  if (!apiKey) throw new Error('DAILY_API_KEY is not set in .env');
  return createDailyRoom(apiKey);
});

ipcMain.handle('minimize-window', () => {
  if (mainWindow) mainWindow.minimize();
});

ipcMain.handle('get-sources', async () => {
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: 320, height: 180 },
  });
  return sources.map((s) => ({
    id: s.id,
    name: s.name,
    thumbnailDataUrl: s.thumbnail.toDataURL(),
  }));
});


app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
